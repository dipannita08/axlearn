# Copyright © 2024 Apple Inc.

"""Base Input interface."""

import math
import re
from typing import Iterable, Iterator, NamedTuple, Optional, Protocol, Union

import jax
from absl import logging
from jax._src.mesh import thread_resources
from jax.sharding import PartitionSpec

from axlearn.common.config import ConfigOr, config_class, maybe_instantiate, maybe_set_config
from axlearn.common.input_dispatch import BaseInputDispatcher, InputDispatcher
from axlearn.common.module import Module
from axlearn.common.utils import (
    Nested,
    Tensor,
    as_numpy_array,
    dispatch_input_batch,
    input_partition_spec,
    tree_paths,
    with_sharding_constraint,
)


class InputPartitionFn(Protocol):
    """Partitions the input batch."""

    def __call__(self, input_batch: Nested[Tensor]) -> Nested[Tensor]:
        """Applies sharding constraints to `input_batch` and returns the modified batch.

        Implementations should avoid making in-place updates to `input_batch`.
        """


class PathAndRank(NamedTuple):
    """A tuple (path, rank) used for matching against inputs in a batch.

    Attributes:
        path: An optional path or path regex. None means match everything.
        rank: An optional rank (ndim). None means match everything.
    """

    path: Optional[Union[str, re.Pattern]]
    rank: Optional[int]


def partition_by_path_rank(
    path_rank_to_partition: dict[PathAndRank, PartitionSpec],
) -> InputPartitionFn:
    """Partitions the paths in the input batch by regex and rank (ndim).

    If not within a mesh, the partition fn is a no-op.

    Args:
        path_rank_to_partition: A mapping from (path_regex, rank) to partition spec.
            For each input path, the Tensor will be constrained by the first matching
            (path_regex, rank) rule, where paths are full-matched against `path_regex` and ranks are
            matched against `rank`.
            `path_regex` or `rank` are allowed to be None to match everything.
            If replication is desired, specify a partition spec of None explicitly.
            If leaving the input unconstrained is desired, specify a partition spec of
            `PartitionSpec.UNCONSTRAINED` explicitly.

    Returns:
        A function that applies sharding constraints to an input batch and returns a new batch.

    Raises:
        ValueError: If no rules match for a given input, which is likely an oversight. If leaving
            inputs unconstrained is desired, explicitly specify `PartitionSpec.UNCONSTRAINED`.

    Example:
        To constrain all rank-1 Tensors by ("data",) and rank-2 by ("data", "seq"):
        ```
        partition_by_path_ndim({
            (".*", 1): PartitionSpec("data"),
            (".*", 2): PartitionSpec("data", "seq"),
        })
        ```
    """
    compiled = {}
    for (regex, rank), spec in path_rank_to_partition.items():
        if regex is not None:
            regex = re.compile(regex)
        compiled[(regex, rank)] = spec

    def fn(input_batch: Nested[Tensor]) -> Nested[Tensor]:
        mesh = thread_resources.env.physical_mesh  # type: ignore
        if mesh.empty or mesh.size == 1:
            return input_batch

        def maybe_constrain(path: str, value: Tensor):
            for (path_regex, rank), partition_spec in compiled.items():
                if not (rank is None or value.ndim == rank) or not (
                    path_regex is None or re.fullmatch(path_regex, path)
                ):
                    continue
                if partition_spec is not PartitionSpec.UNCONSTRAINED:
                    value = with_sharding_constraint(value, partition_spec)
                    logging.log_first_n(
                        logging.INFO,
                        "Constraining input_batch[%s] with %s.",
                        len(input_batch),
                        path,
                        partition_spec,
                    )
                return value
            # No rules match. We raise as not-constraining is likely an oversight.
            raise ValueError(
                f"No rules matched input_batch['{path}']. "
                "If you intended to leave the input unconstrained, "
                "specify `PartitionSpec.UNCONSTRAINED` explicitly."
            )

        return jax.tree.map(maybe_constrain, tree_paths(input_batch), input_batch)

    return fn


class Input(Module):
    """A Module to generate input batches.

    Subclasses typically only need to implement the `dataset` method. See `input_tf_data.Input` and
    `input_grain.Input` for example implementations.

    The typical usage within a trainer is:
    1. Construct an iterator using `iter(input.dataset())`.
    2. Iterate over per-feed physical batches using `batches(iterator)`.
    3. Use `host_to_global_device_array` to construct a global physical batch.
    4. Use `dispatch_global_batch` within pjit to construct a global logical batch.

    Example:
        ```
        input = Input.default_config().set(...).instantiate(parent=None)
        input_iter = iter(input.dataset())  # Construct an iterator (used e.g. for checkpointing).

        def train_step(global_physical_batch):
            global_logical_batch = input.dispatch_global_batch(global_physical_batch)
            ...

        for per_feed_physical_batch in input.batches(input_iter):
            global_physical_batch = host_to_global_device_array(
                per_feed_physical_batch, partition=input.partition_spec
            )
            ... = pjit(train_step)(global_physical_batch)
        ```
    """

    @config_class
    class Config(Module.Config):
        """Configures Input.

        Attributes:
            partition_spec: If not None, configures the partition specs for the input batch used in
                `host_to_global_device_array` and `jit`. Note that these specs may be different from
                those constrained by `input_partitioner`, as they depend on the host-local shapes of
                each input feed. For example, it is common to first form global batches from
                uniformly batch-sharded host-local arrays by only configuring the batch axes of
                `partition_spec`, and then further partition the batches within `jit` via
                `input_partitioner`.
                If None, defaults to `input_partition_spec()`.
            input_dispatcher: If not None, creates an InputDispatcher and uses it for dispatching
                per-feed batches to global batches.
            input_partitioner: If not None, applies additional sharding constraints on each input
                batch during `dispatch_global_batch`.
        """

        # TODO(markblee): Consider allowing PartitionSpec to be a tree-prefix of the input batch,
        # which is more flexible but potentially complicates dispatch.
        partition_spec: Optional[PartitionSpec] = None
        input_dispatcher: Optional[InputDispatcher.Config] = None
        input_partitioner: Optional[ConfigOr[InputPartitionFn]] = None

    def __init__(self, cfg: Config, *, parent: Optional[Module]):
        super().__init__(cfg, parent=parent)
        cfg = self.config
        self._partition_spec = cfg.partition_spec or input_partition_spec()
        if cfg.input_dispatcher is not None:
            self.input_dispatcher: BaseInputDispatcher = (
                self._add_child(  # pytype: disable=annotation-type-mismatch
                    "input_dispatcher",
                    maybe_set_config(cfg.input_dispatcher, partition_spec=cfg.partition_spec),
                )
            )
        self._input_partitioner: Optional[InputPartitionFn] = maybe_instantiate(
            cfg.input_partitioner
        )

    def dataset(self) -> Iterable[Nested[Tensor]]:
        """Returns the input dataset, which should produce per-feed logical batches.

        Each batch is a pytree of arrays which reside on host memory (i.e., leaves can be any array
        type which can be converted to numpy via `as_numpy_array`).

        The dataset should be iterable, i.e., it is expected to support conversion to an iterator
        via `iter(...)`. Although not strictly required, it is recommended for the iterator to be
        checkpointable.
        """
        raise NotImplementedError(type(self))

    def __iter__(self) -> Iterator[Nested[Tensor]]:
        """Iterates over the input dataset.

        The iterator should produce per-feed physical batches (by iterating over the iterable
        returned by `dataset` using `batches()`).

        To obtain a checkpointable iterator, use `iter(dataset())` directly.
        """
        yield from self.batches(iter(self.dataset()))

    def batches(self, it: Iterator[Nested[Tensor]]) -> Iterator[Nested[Tensor]]:
        """Yields per-feed physical input batches (using `input_dispatcher` if configured).

        The caller should use `host_to_global_array` to construct a global physical batch from the
        per-feed physical batches returned from this method.

        See also `dispatch_global_batch` for constructing a global logical batch.
        """
        for input_batch in it:
            input_batch = as_numpy_array(input_batch)
            if "input_dispatcher" in self.children:
                input_batch = self.input_dispatcher.logical_to_physical_batch(input_batch)
            yield input_batch

    def dispatch_global_batch(self, global_physical_batch: Nested[Tensor]) -> Nested[Tensor]:
        """Converts a global physical batch to a global logical batch.

        The leaves of the output logical batch are partitioned across `batch_axis_names` along the
        0th (batch) dimension. This should be invoked from within `pjit` so that the sharding
        constraints can be applied.

        If `cfg.input_partitioner` is not None, it will be applied to each logical batch after
        constraining `batch_axis_names`.
        """

        def constrain_batch_axis(path: str, value: Tensor):
            mesh = thread_resources.env.physical_mesh
            batch_partitions = math.prod(
                mesh.shape[axis] for axis in jax.tree.leaves(self._partition_spec[0])
            )
            # Warn if an invalid constraint is applied, since by default this can silently be
            # ignored, potentially leading to unexpected OOMs.
            if value.shape[0] % batch_partitions != 0:
                logging.warning(
                    "Attempting to constrain path=%s (with batch dim %d) over %d partitions (%s).",
                    path,
                    value.shape[0],
                    batch_partitions,
                    self._partition_spec,
                )
            return with_sharding_constraint(value, self._partition_spec)

        if "input_dispatcher" in self.children:
            global_logical_batch = self.input_dispatcher.physical_to_logical_batch(
                jax.tree.map(
                    constrain_batch_axis,
                    tree_paths(global_physical_batch),
                    global_physical_batch,
                )
            )
        else:
            global_logical_batch = dispatch_input_batch(
                global_physical_batch, batch_axis_names=self._partition_spec[0]
            )

        global_logical_batch = jax.tree.map(
            constrain_batch_axis, tree_paths(global_logical_batch), global_logical_batch
        )

        # Further constrain based on user-configured partitioning rules.
        if self._input_partitioner is not None:
            global_logical_batch = self._input_partitioner(global_logical_batch)

        return global_logical_batch

    def element_spec(self) -> Nested[jax.ShapeDtypeStruct]:
        """Returns the per-feed logical batch spec.

        This is used e.g. for AOT compilation and is not strictly required for training.
        """
        raise NotImplementedError(type(self))

    @property
    def partition_spec(self) -> PartitionSpec:
        """Returns the input partition spec for `host_to_global_device_array` and for `jit`.

        Depending on the dispatch implementation, it may be possible to directly form the global
        logical batch from feed logical batches via `host_to_global_device_array`. In these cases,
        we can use an input partition spec that follows `cfg.partition_spec`.

        In all other cases we default to `input_partition_spec()`.
        """
        if "input_dispatcher" in self.children:
            return self.input_dispatcher.partition_spec
        return input_partition_spec()
