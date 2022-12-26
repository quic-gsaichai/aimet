# /usr/bin/env python3.8
# -*- mode: python -*-
# =============================================================================
#  @@-COPYRIGHT-START-@@
#
#  Copyright (c) 2022, Qualcomm Innovation Center, Inc. All rights reserved.
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are met:
#
#  1. Redistributions of source code must retain the above copyright notice,
#     this list of conditions and the following disclaimer.
#
#  2. Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions and the following disclaimer in the documentation
#     and/or other materials provided with the distribution.
#
#  3. Neither the name of the copyright holder nor the names of its contributors
#     may be used to endorse or promote products derived from this software
#     without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
#  AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
#  IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
#  ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
#  LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
#  CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
#  SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
#  INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
#  CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
#  ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
#  POSSIBILITY OF SUCH DAMAGE.
#
#  SPDX-License-Identifier: BSD-3-Clause
#
#  @@-COPYRIGHT-END-@@
# =============================================================================
""" ONNX Code to fold batch-norm layers """

from typing import Dict, List, Tuple
import contextlib
from onnx import helper, onnx_pb, numpy_helper
import numpy as np

from aimet_common.bias_correction import ConvBnPatternHandler
from aimet_common.graph_pattern_matcher import PatternType
from aimet_common.graph_searcher import GraphSearcher
from aimet_common.connected_graph.connectedgraph_utils import get_ordered_ops
import aimet_common.libpymo as libpymo

from aimet_onnx.meta.connectedgraph import ConnectedGraph
from aimet_onnx.meta.connectedgraph import WEIGHT_INDEX, BIAS_INDEX, RUNNING_MEAN_INDEX, RUNNING_VAR_INDEX
from aimet_onnx.meta.operations import Op
from aimet_onnx.utils import get_node_attribute, remove_node, transpose_tensor, ParamUtils


ConvType = ['Conv', 'ConvTranspose']
LinearType = ['Gemm', 'MatMul']
BatchNormType = ['BatchNormalization']


def _find_conv_bn_pairs(connected_graph: ConnectedGraph) -> Dict:
    """
    Uses searcher to find preceding and next bn layers for a conv/linear layer
    :param connected_graph: ConnectedGraph object.
    :return: dictionary of conv/linear Op with associated bn op / activation info
    """

    # initialize all patterns to be matched and associated call back functions
    patterns_with_callbacks = []
    layer_select_handler = ConvBnPatternHandler()
    preceding_linear_op_types = ['Flatten', 'Reshape']

    # Linear layer combinations
    for preceding_linear_op_type in preceding_linear_op_types:
        # BN -> Linear
        patterns_with_callbacks.append(PatternType(pattern=['BatchNormalization', preceding_linear_op_type, 'Gemm'],
                                                   action=layer_select_handler))
        patterns_with_callbacks.append(PatternType(pattern=['BatchNormalization', preceding_linear_op_type, 'MatMul'],
                                                   action=layer_select_handler))

    for op_type in ConvType + LinearType:
        patterns_with_callbacks.append(PatternType(pattern=['BatchNormalization', op_type],
                                                   action=layer_select_handler))
        patterns_with_callbacks.append(PatternType(pattern=[op_type, 'BatchNormalization'],
                                                   action=layer_select_handler))

    # create graph searcher instance with connected graph and patterns to search
    graph_searcher = GraphSearcher(connected_graph, patterns_with_callbacks)

    # get all conv/linear and bn info
    graph_searcher.find_all_patterns_in_graph_apply_actions()
    convs_bn_activation_dict = layer_select_handler.get_conv_linear_bn_info_dict()

    return convs_bn_activation_dict


def find_all_batch_norms_to_fold(connected_graph: ConnectedGraph,
                                 ) -> Tuple[List[Tuple[onnx_pb.NodeProto, onnx_pb.NodeProto]],
                                            List[Tuple[onnx_pb.NodeProto, onnx_pb.NodeProto]]]:
    """
    Find all possible batch norm layers that can be folded. Returns a list of pairs such that (bn, layer)
    means bn will be forward-folded into layer and (layer, bn) means bn will be backward-folded into layer
    :param connected_graph: connected graph model to search
    :return: A list of (layer, bn) pairs and a list of (bn, layer) pairs,
             where `bn` can be folded into to `layer`.
    """
    conv_linear_bn_activation_info_dict = _find_conv_bn_pairs(connected_graph)
    model = connected_graph.model
    # To mark BN's already picked for backward folding
    bn_picked_for_folding = set()

    ordered_conv_fc_nodes = get_ordered_conv_linears(connected_graph)

    conv_bn_pairs = []
    # Backward fold is given priority over Forward fold
    for node in ordered_conv_fc_nodes:
        # Filter out combinations that are not supported
        if node in conv_linear_bn_activation_info_dict.keys() and is_valid_bn_fold(node.get_module(), model, True):
            bn_info = conv_linear_bn_activation_info_dict[node]
            if bn_info.output_bn and bn_info.output_bn not in bn_picked_for_folding:
                conv_bn_pairs.append((node.get_module(), bn_info.output_bn.get_module()))
                bn_picked_for_folding.add(bn_info.output_bn)

    bn_conv_pairs = []
    for node in ordered_conv_fc_nodes:
        # Filter out combinations that are not supported
        if node in conv_linear_bn_activation_info_dict.keys() and is_valid_bn_fold(node.get_module(), model, False):
            bn_info = conv_linear_bn_activation_info_dict[node]
            if bn_info.input_bn and bn_info.input_bn not in bn_picked_for_folding:
                bn_conv_pairs.append((bn_info.input_bn.get_module(), node.get_module()))
                bn_picked_for_folding.add(bn_info.input_bn)

    return conv_bn_pairs, bn_conv_pairs


def get_ordered_conv_linears(conn_graph: ConnectedGraph) -> List[Op]:
    """
    helper to select a list of candidate layers for BatchNorm folding
    :param conn_graph: connected graph to search
    :return: List of conv/linear layers
    """
    # get ordered operations list from the connected graph
    list_of_ordered_ops = get_ordered_ops(conn_graph.starting_ops)

    # look for conv/linear layers
    ordered_convs = []
    for op in list_of_ordered_ops:
        if op.type in ConvType + LinearType:
            ordered_convs.append(op)
    return ordered_convs


def is_valid_bn_fold(conv: onnx_pb.NodeProto, model: onnx_pb.ModelProto, fold_backward: bool) -> bool:
    """
    Determine if a given layer can successfully absorb a BatchNorm given the layer type and parameters
    :param conv: The Conv/Linear layer to fold a BatchNorm into.
    :param model: The model to which the Conv/Linear layer belongs.
    :param fold_backward: True if BatchNorm comes after Conv/Linear layer
    :return: True if a BatchNorm layer can be folded without causing output error.
    """
    valid = True
    if not fold_backward:
        # Cannot fold BN -> Conv with padding. AIMET does not support forward folding to grouped or DW Conv
        if conv.op_type == 'Conv':
            valid &= all(item == 0 for item in get_node_attribute(conv, "pads"))
            valid &= get_node_attribute(conv, "group") == 1
        # AIMET does not support forward folding to ConvTranspose
        elif conv.op_type == 'ConvTranspose':
            valid = False
    else:
        # AIMET does not support backwards folding to grouped ConvTranspose
        if conv.op_type == 'ConvTranspose':
            valid &= get_node_attribute(conv, "group") in (1, get_input_output_channels(conv, model)[0])
    return valid


def fold_all_batch_norms_to_weight(model: onnx_pb.ModelProto) -> List[Tuple[onnx_pb.NodeProto, onnx_pb.NodeProto]]:
    """
    Fold all possible batch_norm layers in a model into the weight of the corresponding conv layers

    :param model: onnx Model to perform BN fold on
    :return: A list of pairs of layers [(Conv/Linear, BN layer that got folded)]
    """
    connected_graph = ConnectedGraph(model)
    model = connected_graph.model
    conv_bn_pairs, bn_conv_pairs = find_all_batch_norms_to_fold(connected_graph)

    for conv, bn in conv_bn_pairs:
        _fold_to_weight(model, conv, bn, True)
        remove_node(bn, model.graph)

    for bn, conv in bn_conv_pairs:
        _fold_to_weight(model, conv, bn, False)
        remove_node(bn, model.graph)

    return conv_bn_pairs + [(conv, bn) for bn, conv in bn_conv_pairs]


def _fold_to_weight(model: onnx_pb.ModelProto,
                    conv_linear: onnx_pb.NodeProto,
                    bn: onnx_pb.NodeProto,
                    fold_backward: bool):
    """
    Fold BatchNorm into the weight and bias of the given layer.

    :param model: onnx model to which the conv/bn pair belong
    :param conv_linear: Conv or linear layer to fold BN into.
    :param bn: BatchNorm to fold.
    :param fold_backward: True if the BatchNorm comes after the Conv
    """
    # Must convert MatMul layers to Gemm to allow bias
    if conv_linear.op_type == "MatMul":
        _matmul_to_gemm(conv_linear, model)

    weight = ParamUtils.get_param(model, conv_linear, WEIGHT_INDEX)
    bias = ParamUtils.get_param(model, conv_linear, BIAS_INDEX)
    groups = get_node_attribute(conv_linear, "group")

    # If layer doesn't have bias, create a bias initializer and add it to the model, then retrieve it
    if not bias:
        bias_data = np.zeros(get_input_output_channels(conv_linear, model)[1])
        bias_name = conv_linear.name + ".bias"
        bias = numpy_helper.from_array(bias_data.astype(np.float32), name=bias_name)
        model.graph.initializer.append(bias)
        conv_linear.input.append(bias_name)
        bias = ParamUtils.get_param(model, conv_linear, BIAS_INDEX)

    # Transpose weights to C, N, H, W from N, C, H, W since axis are flipped for transposed conv
    # However depthwise conv layers are always N, 1, H, W whether transposed-conv or not, so no need to transpose
    # if conv_linear.type == "ConvTranspose" and conv_linear groups == 1:
    if conv_linear.op_type == "ConvTranspose" and groups == 1:
        weight = transpose_tensor(weight, (1, 0, 2, 3))
    # Gemm layers may or may not need to have weights transposed depending on value of transB attribute
    elif conv_linear.op_type in LinearType and not get_node_attribute(conv_linear, "transB"):
        weight = transpose_tensor(weight, (1, 0))

    _call_mo_batch_norm_fold(model, weight, bias, bn, fold_backward=fold_backward)

    # Transpose weight back to original configuration
    if conv_linear.op_type == "ConvTranspose" and groups == 1:
        weight = transpose_tensor(weight, (1, 0, 2, 3))
    elif conv_linear.op_type in LinearType and not get_node_attribute(conv_linear, "transB"):
        weight = transpose_tensor(weight, (1, 0))

    weight_param = ParamUtils.get_param(model, conv_linear, WEIGHT_INDEX)
    weight_param.raw_data = weight.raw_data


def _matmul_to_gemm(node: onnx_pb.NodeProto, model: onnx_pb.ModelProto):
    """
    Convert MatMul node to Gemm and initialize bias to zeros

    :param node: MatMul node to convert to Gemm
    :param model: model to which the node belongs
    """
    assert node.op_type == "MatMul"

    weight_input = node.input[WEIGHT_INDEX]
    weight = ParamUtils.get_param(model, node, WEIGHT_INDEX)
    transposed = False
    # Check if the weight is transposed before entering the node
    for other_node in model.graph.node:
        if weight_input in other_node.output and other_node.op_type == "Transpose":
            node.input[WEIGHT_INDEX] = other_node.input[0]
            weight = ParamUtils.get_param(model, node, WEIGHT_INDEX)
            transposed = True
    if not transposed:
        model.graph.initializer.remove(weight)
        weight = transpose_tensor(weight, (1, 0))
        model.graph.initializer.append(weight)
    trans_b = helper.make_attribute("transB", 1)
    node.attribute.append(trans_b)
    node.op_type = "Gemm"
    node.name = node.name.replace("MatMul", "Gemm")
    # Create bias vector for Gemm operation
    bias_name = node.name + ".bias"
    bias_data = np.zeros(weight.dims[1]) if trans_b is None else np.zeros(weight.dims[0])
    bias = numpy_helper.from_array(bias_data.astype(np.float32), name=bias_name)
    model.graph.initializer.append(bias)
    node.input.append(bias_name)


def _call_mo_batch_norm_fold(model: onnx_pb.ModelProto,
                             weight: onnx_pb.TensorProto,
                             bias: onnx_pb.TensorProto,
                             bn: onnx_pb.NodeProto,
                             fold_backward: bool):
    """
    Calls C++ batch norm folding API.

    :param model: onnx model containing the (conv, bn) pair to be folded
    :param weight: Weight or scale tensor to fold BN into.
    :param bias: Bias tensor to fold BN into.
    :param bn: Batch Norm layer
    :param fold_backward: True if BatchNorm comes after Conv/Linear layer
    """
    channels = weight.dims[0] if fold_backward else weight.dims[1]
    bn_params = get_bn_params(model, bn, channels)

    weight_tensor = libpymo.TensorParams()

    weight_tensor.data = numpy_helper.to_array(weight).reshape(-1)
    weight_tensor.shape = np.array(weight.dims)

    bias_tensor = libpymo.TensorParams()

    bias_tensor.data = numpy_helper.to_array(bias).reshape(-1)
    bias_tensor.shape = np.array(bias.dims)
    is_bias_valid = True

    with _expand_shape_to_4d(weight_tensor):
        _bias = libpymo.fold(bn_params, weight_tensor, bias_tensor, is_bias_valid, fold_backward)

    bias.raw_data = np.asarray(_bias, dtype=np.float32).tobytes()
    weight.raw_data = np.asarray(weight_tensor.data, dtype=np.float32).tobytes()


def get_bn_params(model: onnx_pb.ModelProto, bn: onnx_pb.NodeProto, channels: int) -> libpymo.BNParams:
    """
    Returns the populated libpymo.BNParams object for the given BatchNormalization layer with
    parameters repeated if necessary.

    :param model: model to which the bn layer belongs
    :param bn: BatchNormalization layer to retrieve the parameters from
    :param channels: The effective number of channels the BatchNorm layer operates on (needed for Gemm layers)
    :return: libpymo.BNParams object for the input BatchNorm layer
    """
    bn_params = libpymo.BNParams()
    gamma = numpy_helper.to_array(ParamUtils.get_param(model, bn, WEIGHT_INDEX)).reshape(-1)
    # In the case of BatchNorm2d -> Flatten -> Gemm, must resize the BN parameters to the Gemm input feature length
    resize = channels / len(gamma)
    bn_params.gamma = np.repeat(gamma, resize)
    bn_params.beta = np.repeat(numpy_helper.to_array(ParamUtils.get_param(model, bn, BIAS_INDEX)).reshape(-1), resize)
    bn_params.runningMean = np.repeat(
        numpy_helper.to_array(ParamUtils.get_param(model, bn, RUNNING_MEAN_INDEX)).reshape(-1), resize)
    runningVar = numpy_helper.to_array(ParamUtils.get_param(model, bn, RUNNING_VAR_INDEX))

    epsilon = get_node_attribute(bn, "epsilon")
    sigma = np.sqrt(runningVar + epsilon)
    bn_params.runningVar = np.repeat(sigma.reshape(-1), resize)

    return bn_params


@contextlib.contextmanager
def _expand_shape_to_4d(weight_tensor: libpymo.TensorParams):
    """ Expand the shape of the weight into 4d.  """
    dims = len(weight_tensor.shape)

    if dims > 4:
        raise RuntimeError

    if dims == 4:
        yield weight_tensor

    else:
        orig_shape = weight_tensor.shape
        _4d_shape = np.append(orig_shape, [1 for _ in range(4-dims)]).astype(int)
        try:
            weight_tensor.shape = _4d_shape
            yield weight_tensor
        finally:
            weight_tensor.shape = orig_shape


def get_input_output_channels(node: onnx_pb.NodeProto, model: onnx_pb.ModelProto) -> Tuple[int, int]:
    """
    Find the input and output channels of a given layer.
    :param node: The node to find the input/output channels of
    :param model: The onnx model to which the layers belong
    :return: Tuple of (num channels in, num channels out)
    """
    weight = ParamUtils.get_param(model, node, WEIGHT_INDEX)
    groups = get_node_attribute(node, "group")
    if node.op_type == "Conv":
        num_in_channels = weight.dims[1] * groups
        num_out_channels = weight.dims[0]
    elif node.op_type == "ConvTranspose":
        num_in_channels = weight.dims[0]
        num_out_channels = weight.dims[1] * groups
    elif node.op_type == "Gemm":
        transB = get_node_attribute(node, "transB")
        if transB == 1:
            num_out_channels = weight.dims[0]
            num_in_channels = weight.dims[1]
        else:
            num_out_channels = weight.dims[1]
            num_in_channels = weight.dims[0]
    else:
        num_out_channels = None
        num_in_channels = None
    return num_in_channels, num_out_channels