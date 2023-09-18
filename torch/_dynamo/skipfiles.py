import _collections_abc
import _weakrefset
import abc
import collections
import contextlib
import copy
import copyreg
import dataclasses
import enum
import functools
import glob
import importlib
import inspect
import linecache
import logging
import multiprocessing
import operator
import os
import posixpath
import random
import re
import selectors
import signal
import tempfile
import threading
import tokenize
import traceback
import types
import typing
import unittest
import weakref
from typing import Optional

import torch
import torch._inductor.test_operators
import torch.distributed
import torch.utils._content_store

from . import comptime, external_utils

"""
A note on skipfiles:

Dynamo consults this file to determine whether code should be compiled or skipped.

A skip applies at the frame boundary, meaning dynamo either triggers a graph break
at the beginning of the frame or attempts to trace the whole frame.  When skipping
a frame, recursively called frames are still traced by dynamo unless also skipped.

Skipfiles (skipped at the file level instead of function level) still apply on a
frame-by-frame boundary as dynamo traces, but apply to all functions in that file.

@skip is a helper decorator that can be applied to your function to cause it to be
included here.
"""


def _strip_init_py(s):
    return re.sub(r"__init__.py$", "", s)


def _module_dir(m: types.ModuleType):
    return _strip_init_py(m.__file__)


FILENAME_ALLOWLIST = {
    torch.nn.Sequential.__init__.__code__.co_filename,
    torch.set_rng_state.__code__.co_filename,
    torch._inductor.test_operators.__file__,
    torch.utils._content_store.__file__,
    # These are dynamo files!
    external_utils.__file__,
    comptime.__file__,  # Want to inline these helpers
    torch.optim._functional.__file__,
    torch.utils._foreach_utils.__file__,
    _module_dir(torch) + "ao/quantization/pt2e/qat_utils.py",
    _module_dir(torch) + "ao/quantization/quantizer/xnnpack_quantizer.py",
    _module_dir(torch) + "ao/quantization/pt2e/representation/rewrite.py",
    _module_dir(torch) + "ao/quantization/pt2e/utils.py",
    _module_dir(torch) + "ao/quantization/pt2e/eval_utils.py",
    _module_dir(torch) + "_export/constraints.py",
    _module_dir(torch) + "_higher_order_ops/cond.py",
    _module_dir(torch) + "_functorch/apis.py",
    _module_dir(torch) + "_functorch/deprecated.py",
    _module_dir(torch) + "distributed/tensor/parallel/_utils.py",
    _module_dir(torch) + "distributed/tensor/parallel/style.py",
    _module_dir(torch) + "distributed/tensor/parallel/_data_parallel_utils.py",
    _module_dir(torch) + "distributed/_tensor/api.py",
    _module_dir(torch) + "distributed/_tensor/device_mesh.py",
    torch.jit._trace.__file__,
    torch.distributions.normal.__file__,
    torch.distributions.independent.__file__,
    torch.distributions.utils.__file__,
    torch.utils._contextlib.__file__,
    torch.fx._pytree.__file__,
}

if torch.distributed.is_available():
    # Inline the checkpoint code from distributed
    import torch.distributed.algorithms._checkpoint.checkpoint_wrapper

    FILENAME_ALLOWLIST |= {
        torch.distributed.algorithms._checkpoint.checkpoint_wrapper.__file__
    }

# Include optimizer code for tracing
FILENAME_ALLOWLIST |= {
    inspect.getfile(obj)
    for obj in torch.optim.__dict__.values()
    if inspect.isclass(obj)
}

# TODO (zhxchen17) Make exportdb importable here.
FILENAME_ALLOWLIST |= set(
    glob.glob(_module_dir(torch) + "_export/db/examples/*.py"),
) | {
    _module_dir(torch) + "_export/wrappers.py",
}


# inline objects from it or its children
SUBMODULE_ALLOWLIST = {
    torch.nn,
    torch.distributions,
    torch.testing,
    torch.ao.nn,
    torch._refs,
    torch._prims,
    torch._decomp,
    torch.utils._contextlib,
    torch.utils._pytree,
    torch.fx._pytree,
    torch.sparse,
}

if torch.distributed.is_available():
    from torch.distributed import _functional_collectives

    SUBMODULE_ALLOWLIST.add(_functional_collectives)


SKIP_DIRS = [
    # torch.*
    _module_dir(torch),
    # torchdynamo.*
    os.path.dirname(__file__) + "/",
    "<frozen importlib",
    "<__array_function__ internals>",
] + [
    # skip some standard libs
    _module_dir(m)
    for m in (
        abc,
        collections,
        contextlib,
        copy,
        copyreg,
        dataclasses,
        enum,
        functools,
        importlib,
        inspect,
        linecache,
        logging,
        multiprocessing,
        operator,
        os,
        posixpath,
        random,
        re,
        selectors,
        signal,
        tempfile,
        threading,
        tokenize,
        traceback,
        types,
        typing,
        unittest,
        weakref,
        _collections_abc,
        _weakrefset,
    )
]

SKIP_DIRS_RE = None

is_fbcode = importlib.import_module("torch._inductor.config").is_fbcode()
# Skip fbcode paths(including torch.package paths) containing
# one of the following strings.
FBCODE_SKIP_DIRS = {
    "torchrec/distributed",
    "torchrec/fb/distributed",
    "caffe2/torch/fb/sparsenn/pooled_embeddings_modules.py",
}
FBCODE_SKIP_DIRS_RE = re.compile(f".*({'|'.join(map(re.escape, FBCODE_SKIP_DIRS))})")


def _recompile_re():
    global SKIP_DIRS_RE
    SKIP_DIRS_RE = re.compile(f"^({'|'.join(map(re.escape, SKIP_DIRS))})")


def add(import_name: str):
    if isinstance(import_name, types.ModuleType):
        return add(import_name.__name__)
    assert isinstance(import_name, str)
    module_spec = importlib.util.find_spec(import_name)
    if not module_spec:
        return
    origin = module_spec.origin
    if origin is None:
        return
    global SKIP_DIRS_RE
    SKIP_DIRS.append(_strip_init_py(origin))
    _recompile_re()


@dataclasses.dataclass
class SkipResult:
    skipped: bool
    reason: Optional[str]


def check(filename):
    """Should skip this file?"""
    if filename is None:
        return SkipResult(True, "filename is None")
    if filename in FILENAME_ALLOWLIST:
        return SkipResult(
            False,
            "allowlisted in skipfiles.FILENAME_ALLOWLIST",
        )
    if is_torch_inline_allowed(filename):
        return SkipResult(
            False,
            "allowlisted in skipfiles.SUBMODULE_ALLOWLIST",
        )
    if is_fbcode and bool(FBCODE_SKIP_DIRS_RE.match(filename)):
        return SkipResult(
            True,
            "should be skipped according skipfiles.FBCODE_SKIP_DIRS",
        )
    if bool(SKIP_DIRS_RE.match(filename)):
        return SkipResult(True, "should be skipped according skipfiles.SKIP_DIRS")
    else:
        return SkipResult(False, "inlining by default")


# skip common third party libs
for _name in (
    "functorch",
    "fx2trt_oss",
    "intel_extension_for_pytorch",
    "networkx",
    "numpy",
    "omegaconf",
    "onnx",
    "onnxruntime",
    "onnx_tf",
    "pandas",
    "sklearn",
    "tabulate",
    "tensorflow",
    "tensorrt",
    "torch2trt",
    "tqdm",
    "tree",
    "tvm",
    "xarray",
):
    add(_name)

_recompile_re()


def is_torch_inline_allowed(filename):
    return any(filename.startswith(_module_dir(mod)) for mod in SUBMODULE_ALLOWLIST)


@functools.lru_cache(None)
def dynamo_dir():
    import torch._dynamo

    return _module_dir(torch._dynamo)


def is_torch(filename):
    if filename.startswith(dynamo_dir()):
        return False
    return filename.startswith(_module_dir(torch))
