# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from .optimizer import Optimizer
from .adam import Adam
from ..fluid import framework
import paddle
__all__ = ['AdamW']


class DecoupledWeightDecay(object):
    def __init__(self, coeff=0.0, apply_decay_param_fun=None, **kwargs):
        if not isinstance(coeff, float) and \
                not isinstance(coeff, framework.Variable):
            raise TypeError("coeff should be float or Tensor.")
        self._params_name = set()
        self._apply_decay_param_fun = apply_decay_param_fun
        self._coeff = coeff
        super(DecoupledWeightDecay, self).__init__(**kwargs)

    def _scale_parameters(self, params_and_grads):
        """
        Adds weight decay ops.
            scaled_parameter = parameter * coeff

        Args:
            params_and_grads: A list of (parameters, gradients) pairs,
                the parameters need to decay.
        Raises:
            Exception: The type of coeff and parameter is not consistent.
        """
        if isinstance(self._coeff, float) and self._coeff == 0.0:
            return

        scaled_params = []
        for param, grad in params_and_grads:
            # If no gradient then we don't need to do anything
            if grad is None:
                continue
            if self._apply_decay_param_fun is not None \
                    and not self._apply_decay_param_fun(param.name):
                continue

            if isinstance(self._coeff, float):
                assert param.dtype is not paddle.fluid.core.VarDesc.VarType.FP32, \
                    "the type of coeff(float) and parameter(%s) is not consistent."%(self._coeff.dtype)
            else:
                assert self._coeff.dtype == param.dtype, \
                    "the type of coeff(%s) and parameter(%s) is not consistent."%(self._coeff.dtype, param.dtype)

            with param.block.program._optimized_guard(
                [param, grad]), framework.name_scope('weight decay'):
                assert param.name not in self._params_name
                scaled_params.append((param, grad, param * self._coeff))
                self._params_name.add(param.name)
        return scaled_params

    def backward(self, **kargs):
        return super(DecoupledWeightDecay, self).backward(**kargs)

    def apply_optimize(self, **kargs):
        return super(DecoupledWeightDecay, self).apply_optimize(**kargs)

    def minimize(self,
                 loss,
                 startup_program=None,
                 parameters=None,
                 no_grad_set=None):
        params_grads = self.backward(
            loss=loss,
            startup_program=startup_program,
            parameters=parameters,
            no_grad_set=no_grad_set)
        scaled_params = self._scale_parameters(params_grads)
        for p_grad_sgrad in scaled_params:
            param, grad, scaled_param = p_grad_sgrad
            with param.block.program._optimized_guard(
                [param, grad]), framework.name_scope('weight decay'):
                updated_param = paddle.fluid.layers.elementwise_sub(
                    x=param, y=scaled_param)
                paddle.fluid.layers.assign(input=updated_param, output=param)

        optimize_ops = self.apply_optimize(
            loss=loss,
            params_grads=params_grads,
            startup_program=startup_program)
        return optimize_ops, params_grads

    @framework.dygraph_only
    def step(self):
        parameter_list = self._parameter_list
        self._dtype = None
        params_grads = []
        for param in self._parameter_list:
            if not param.trainable:
                continue
            if param._grad_ivar() is not None:
                grad_var = param._grad_ivar()
                params_grads.append((param, grad_var))

        scaled_params = self._scale_parameters(params_grads)
        for p_grad_sgrad in scaled_params:
            param, grad, scaled_param = p_grad_sgrad
            with param.block.program._optimized_guard(
                [param, grad]), framework.name_scope('weight decay'):
                updated_param = paddle.fluid.layers.elementwise_sub(
                    x=param, y=scaled_param)
                paddle.fluid.layers.assign(input=updated_param, output=param)
        optimize_ops = self.apply_optimize(
            loss=None, startup_program=None, params_grads=params_grads)

    def __str__(self):
        return " ".join(["Weight Decay, params:", ",".join(self._params_name)])


class AdamW(DecoupledWeightDecay, Adam):
    """
    The AdamW optimizer is implemented based on the AdamW Optimization 
    in paper `DECOUPLED WEIGHT DECAY REGULARIZATION <https://arxiv.org/pdf/1711.05101.pdf>`_.
    it can resolves the problem of L2 regularization failure in the Adam optimizer.

    .. math::

        t & = t + 1

        moment\_1\_out & = {\\beta}_1 * moment\_1 + (1 - {\\beta}_1) * grad
        
        moemnt\_2\_out & = {\\beta}_2 * moment\_2 + (1 - {\\beta}_2) * grad * grad

        learning\_rate & = learning\_rate * \\
            \\frac{\sqrt{1 - {\\beta}_2^t}}{1 - {beta}_1^t}

        param\_out & = param - learning\_rate * (\\frac{moment\_1}{\sqrt{moment\_2} + \epsilon} + \lambda * param)


    Args:
        learning_rate (float|Tensor, optional): The learning rate used to update ``Parameter``.
            It can be a float value or a ``Tensor`` with a float type. The default value is 0.001.
        beta1 (float|Tensor, optional): The exponential decay rate for the 1st moment estimates.
            It should be a float number or a Tensor with shape [1] and data type as float32.
            The default value is 0.9.
        beta2 (float|Tensor, optional): The exponential decay rate for the 2nd moment estimates.
            It should be a float number or a Tensor with shape [1] and data type as float32.
            The default value is 0.999.
        epsilon (float, optional): A small float value for numerical stability.
            The default value is 1e-08.
	parameters (Iterable, optional): Iterable of ``Tensor`` names to update to minimize ``loss``. \
	    This parameter is required in dygraph mode. \
	    The default value is None in static mode, at this time all parameters will be updated.
        weight_decay (float|Tensor): The weight decay coefficient, it can be
            float or Tensor.
        apply_decay_param_fun (function|None): If it is not None,
            only tensors that makes apply_decay_param_fun(Tensor)==True 
            will be updated. It only works when we want to specify tensors.
            Default: None.
        grad_clip (GradientClipBase, optional): Gradient cliping strategy, it's an instance of 
            some derived class of ``GradientClipBase`` . There are three cliping strategies 
            ( :ref:`api_fluid_clip_GradientClipByGlobalNorm` , :ref:`api_fluid_clip_GradientClipByNorm` , 
            :ref:`api_fluid_clip_GradientClipByValue` ). Default None, meaning there is no gradient clipping.
        name (str, optional): Normally there is no need for user to set this property.
            For more information, please refer to :ref:`api_guide_Name`.
            The default value is None.
        lazy_mode (bool, optional): The official Adam algorithm has two moving-average accumulators.
            The accumulators are updated at every step. Every element of the two moving-average
            is updated in both dense mode and sparse mode. If the size of parameter is very large,
            then the update may be very slow. The lazy mode only update the element that has
            gradient in current mini-batch, so it will be much more faster. But this mode has
            different semantics with the original Adam algorithm and may lead to different result.
            The default value is False.
    **Notes**:
        **Currently, AdamW doesn't support sparse parameter optimization.**

    Examples:
        .. code-block:: python

          import paddle.fluid as fluid
          import paddle
          import numpy

          # First create the Executor.
          place = fluid.CPUPlace() # fluid.CUDAPlace(0)
          exe = fluid.Executor(place)

          train_program = fluid.Program()
          startup_program = fluid.Program()
          with fluid.program_guard(train_program, startup_program):
              data = fluid.data(name='X', shape=[None, 1], dtype='float32')
              hidden = fluid.layers.fc(input=data, size=10)
              loss = fluid.layers.mean(hidden)
              adam = paddle.optimizer.AdamW(learning_rate=0.2)
              adam.minimize(loss)

          # Run the startup program once and only once.
          exe.run(startup_program)

          x = numpy.random.random(size=(10, 1)).astype('float32')
          outs = exe.run(program=train_program,
                        feed={'X': x},
                         fetch_list=[loss.name])
    """

    def __init__(self, weight_decay, apply_decay_param_fun=None, **kwargs):
        super(AdamW, self).__init__(
            weight_decay, apply_decay_param_fun=apply_decay_param_fun, **kwargs)
