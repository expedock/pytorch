import dataclasses
import itertools

import sympy

import torch._inductor.virtualized as virtualized
from torch._inductor.scheduler import BaseSchedulerNode
from torch._inductor.utils import sympy_str, IndentedBuffer


def _arg_str(a):
    if isinstance(a, sympy.Expr):
        return "sympy_expr('" + sympy_str(a) + "')"
    return str(a)

class CutlassEVTEpilogueTypeFormatter():
    """
    Replacement for V.KernelFormatterHandler
    """

    def __init__(self, accumulator_node_name):
        self.accumulator_node_name = accumulator_node_name
        self.output = IndentedBuffer(0)
        self.var_counter = 0
        self.aliases = dict()

    @staticmethod
    def ir_to_evt_string(template_node : ir.Node, evt_type_name, epilogue_nodes : List[ir.Node], index, rindex=None):
        args = [index, rindex] if rindex is not None else [index]
        formatter = CutlassEVTEpilogueTypeFormatter(template_node.name)

        with virtualized.V.set_ops_handler(formatter):
            for node in epilogue_nodes:
                result = node.ir_fn(*args)
                formatter.aliases[node.name] = result
            return formatter.getvalue(result)

    def __getattr__(self, name):
        def inner(*args, **kwargs):
            fargs = [_arg_str(a) for a in args]
            fkwargs = { key : _arg_str(a) for key, a in kwargs.items() }
            fn = getattr(self, f"_op_{name}")
            line = fn(*fargs, **fkwargs)
            # replace line with a new variable name
            varname = f"EVT_expr_{self.var_counter}"
            self.var_counter += 1
            self.output.writeline(f"using {varname} = {line};")
            return varname

        if hasattr(self, f"_op_{name}"):
            return inner
        else:
            raise NotImplementedError(name)

    def load(self, name, index_expr):
        if name==self.accumulator_node_name:
            return f"cutlass::epilogue::fusion::Sm90AccFetch /* :={name} */"
        elif name in self.aliases:
            return self.aliases[name]
        else:
            return f"cutlass::epilogue::fusion::Sm90SrcFetch /* :={name} */"

    def constant(self, value, dtype):
        if str(dtype) in ('torch.float16', 'torch.float32'):
            return f"cutlass::epilogue::fusion::Sm90ScalarBroadcast<ElementScalar> /* value={value}, dtype={dtype} */"
        else:
            raise NotImplementedError(f"Unsupported dtype for constant: {dtype}")
    def _cutlass_binary_functional_op(self, op, a, b):
        # see https://github.com/NVIDIA/cutlass/blob/6407bcdf0a24097b7b016ee105937693c62f9923/include/cutlass/functional.h for ops
        return f"cutlass::epilogue::fusion::Sm90EVT<cutlass::epilogue::fusion::Sm90Compute<cutlass::{op}, ElementCompute, ElementCompute, RoundStyle>,{a},{b}>"
    def _op_mul(self, a, b):
        return self._cutlass_binary_functional_op("multiplies", a, b)
    def _op_ge(self, a, b):
        return self._cutlass_binary_functional_op("greater_equal", a, b)
    def _op_add(self, a, b):
        return self._cutlass_binary_functional_op("plus", a, b)
    def _op_sub(self, a, b):
        return self._cutlass_binary_functional_op("minus", a, b)

    def reduction(self, dtype, src_dtype, reduction_type, value):
        raise NotImplementedError()
    def getvalue(self, result):
        self.output.writeline(f"using CustomEVT = EVT_expr_{self.var_counter};")
        return self.output.getvalue()


#
# Copied and modified from https://github.com/NVIDIA/cutlass/blob/main/tools/library/scripts/gemm_operation.py
