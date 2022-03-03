import time
from dataclasses import dataclass
from typing import Tuple

import psutil
import torch
import torch.nn as nn

from oslo.pytorch.utils.huggingface import is_huggingface_model

HF_BATCH_DIMENSIONS = {
    "input_ids": 0,
    "attention_mask": 0,
    "token_type_ids": 0,
    "position_ids": 0,
    "head_mask": None,
    "inputs_embeds": 0,
    "labels": 0,
    "use_cache": None,
    "output_attentions": None,
    "output_hidden_states": None,
    "return_dict": None,
}


@dataclass
class Segment(object):
    # ``S`` in the paper.
    modules: Tuple[nn.Module]

    @property
    def cost(self):
        return sum([m.cost for m in self.modules])


@dataclass
class Partition(object):
    # ``P`` in the paper.
    segments: Tuple[Segment]

    @property
    def cost(self):
        return sum([m.cost for m in self.segments])


class PipelineParallelEngine(object):
    """
    For more information of the implementation, see the following paper.

    References:
        Amazon SageMaker Model Parallelism: A General and Flexible Framework for Large Model Training
        https://arxiv.org/abs/2111.05972
    """

    def __init__(
        self, model, mpu, tracing_inputs=None, memory_computation_balance_factor=1.0
    ):
        self.model = model
        self.mpu = mpu
        self.tracing_inputs = tracing_inputs

        if tracing_inputs is None and not is_huggingface_model(model):
            raise ValueError(
                "`tracing_inputs` must not be None "
                "if the model is not Hugging Face Transformers model"
            )

        # 1. compute the partitioning cost
        cost_estimator = PartitioningCostEstimator(
            root_node=self.model,
            alpha=memory_computation_balance_factor,
            tracing_inputs=tracing_inputs,
        )
        cost_estimator.compute_cost()

        # 2. Do partitioning
        self.initialize_partition()

    def initialize_partition(self):
        # The algorithm starts with a set of virtual devices
        # P(r) = {0, 1, . . . , D − 1} for the root node r
        initial_partition = [
            p for p in range(self.mpu.get_pipeline_parallel_world_size())
        ]
        # P(n)
        setattr(self.model, "oslo_pp_device_cands", initial_partition)
        # d(n)
        setattr(self.model, "oslo_pp_device", self.model.oslo_pp_device_cands[0])

    def make_segments(self, node):
        L = self.mpu.get_pipeline_parallel_world_size()
        len_Q = len([_ for _ in node.children()])

        for i in range(0, len_Q):
            for k in range(2, L + 1):
                pass


class PartitioningCostEstimator(object):
    """
    Partitioning cost estimator

    1. computation cost: supports only the cpu time estimating in this version.
    2. memory cost: computes memory cost via the number of parameters of the module.
    """

    def __init__(self, root_node, alpha, tracing_inputs):
        self.root_node = root_node
        self.alpha = alpha
        self.tracing_inputs = tracing_inputs

        self.hooks = []
        if is_huggingface_model(root_node):
            self.orig_gradient_checkpointing_status = (
                self.root_node.is_gradient_checkpointing
            )

            # enable gradient checkpointing for memory safer tracing.
            if self.root_node.supports_gradient_checkpointing:
                self.root_node.gradient_checkpointing_enable()

        # prevent tracing for very large model.
        if self.alpha < 1.0:
            if not self._is_available_tracing(self.root_node):
                print(
                    "This model is too large to trace on the CPU."
                    "turn off computation cost estimating."
                )
                self.use_computation_cost = False
            else:
                self._trace_computation_cost(tracing_inputs)

    @staticmethod
    def _is_available_tracing(module):
        elem_size = torch.zeros(1, dtype=module.dtype).element_size()
        model_memory_size = (
            sum(p.numel() for p in module.parameters() if p.requires_grad) * elem_size
        )
        available_memory_size = psutil.virtual_memory().available
        return available_memory_size > model_memory_size * 2
        # multiply by 2 to consider act memory for safer tracing.

    def _add_computation_cost_hooks(self, node):
        # TODO: The time unit is not mentioned in the paper.
        # I sent an email to author of paper. but he didn't reply :(

        def pre_hook(*args, **kwargs):
            setattr(node, "execution_time_before_forwarding", time.time())

        def post_hook(*args, **kwargs):
            setattr(
                node,
                "oslo_pp_computation_cost",
                time.time() - getattr(node, "execution_time_before_forwarding"),
            )
            delattr(node, "execution_time_before_forwarding")

        self.hooks.append(
            {
                "pre_hook": node.module.register_forward_pre_hook(pre_hook),
                "post_hook": node.module.register_forward_hook(post_hook),
            }
        )

        for child in node.children():
            self._add_computation_cost_hooks(child)

    def _trace_computation_cost(self, tracing_inputs):
        # 1. tracing the model
        with torch.no_grad():
            if tracing_inputs is None:
                tracing_inputs = self.root_node.dummy_inputs
                tracing_inputs["use_cache"] = False  # for checkpointing

            self._add_computation_cost_hooks(self.root_node)
            self.root_node(**tracing_inputs)

        # 2. removing hooks
        for hooks in self.hooks:
            hooks["pre_hook"].remove()
            hooks["post_hook"].remove()

        # 3. turn off gradient checkpointing
        if not self.orig_gradient_checkpointing_status:
            self.root_node.gradient_checkpointing_disable()

    def _compute_node_cost(self, node):
        # 1. compute memory cost
        memory_cost = sum(p.numel() for p in node.parameters() if p.requires_grad)

        # 2. compute computation cost if available
        computation_cost = (
            node.computation_cost if hasattr(node, "oslo_pp_computation_cost") else 0.0
        )

        # 3. compute total cost
        total_cost = (self.alpha * memory_cost) + ((1 - self.alpha) * computation_cost)

        setattr(node, "oslo_pp_unnormalized_cost", total_cost)

        if hasattr(node, "oslo_pp_computation_cost"):
            delattr(node, "oslo_pp_computation_cost")

    def _compute_cost(self, node):
        if not hasattr(self.root_node, "oslo_pp_unnormalized_cost"):
            # 1. compute cost for root node
            self._compute_node_cost(self.root_node)
        else:
            # 2. compute cost for children nodes
            self._compute_node_cost(node)

        # 3. do recursion
        for child in node.children():
            self._compute_cost(child)

    def _normalize_cost(self, node):
        if not hasattr(self.root_node, "oslo_pp_cost"):
            # 1. normalize cost for root node
            setattr(self.root_node, "oslo_pp_cost", 1.0)
        else:
            # 2. normalize cost for children nodes
            root_cost = getattr(self.root_node, "oslo_pp_unnormalized_cost")
            node_cost = getattr(node, "oslo_pp_unnormalized_cost")
            setattr(node, "oslo_pp_cost", node_cost / root_cost)
            delattr(node, "oslo_pp_unnormalized_cost")

        # 3. do recursion
        for child in node.children():
            self._normalize_cost(child)

    def compute_cost(self):
        # 1. compute cost
        self._compute_cost(self.root_node)

        # 2. normalize cost
        self._normalize_cost(self.root_node)
        delattr(self.root_node, "oslo_pp_unnormalized_cost")
