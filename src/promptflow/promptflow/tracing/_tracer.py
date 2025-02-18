import json
import inspect
import logging
import uuid
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import datetime
from typing import Dict, List, Optional

from promptflow._core.generator_proxy import GeneratorProxy, generate_from_proxy
from promptflow._core.thread_local_singleton import ThreadLocalSingleton
from promptflow._utils.dataclass_serializer import serialize
from promptflow.contracts.tool import ConnectionType

from .._utils.utils import default_json_encoder
from .contracts.trace import Trace, TraceType


class Tracer(ThreadLocalSingleton):
    CONTEXT_VAR_NAME = "Tracer"
    context_var = ContextVar(CONTEXT_VAR_NAME, default=None)

    def __init__(self, run_id, node_name: Optional[str] = None):
        self._run_id = run_id
        self._node_name = node_name
        self._traces = []
        self._current_trace_id = ContextVar("current_trace_id", default="")
        self._id_to_trace: Dict[str, Trace] = {}

    @classmethod
    def start_tracing(cls, run_id, node_name: Optional[str] = None):
        current_run_id = cls.current_run_id()
        if current_run_id is not None:
            msg = f"Try to start tracing for run {run_id} but {current_run_id} is already active."
            logging.warning(msg)
            return
        tracer = cls(run_id, node_name)
        tracer._activate_in_context()

    @classmethod
    def current_run_id(cls):
        tracer = cls.active_instance()
        if not tracer:
            return None
        return tracer._run_id

    @classmethod
    def end_tracing(cls, run_id: Optional[str] = None):
        tracer = cls.active_instance()
        if not tracer:
            return []
        if run_id is not None and tracer._run_id != run_id:
            return []
        tracer._deactivate_in_context()
        return tracer.to_json()

    @classmethod
    def push(cls, trace: Trace):
        obj = cls.active_instance()
        if not obj:
            return
        obj._push(trace)

    @staticmethod
    def to_serializable(obj):
        if isinstance(obj, dict) and all(isinstance(k, str) for k in obj.keys()):
            return {k: Tracer.to_serializable(v) for k, v in obj.items()}
        if isinstance(obj, GeneratorProxy):
            return obj
        try:
            obj = serialize(obj)
            json.dumps(obj, default=default_json_encoder)
        except Exception:
            # We don't want to fail the whole function call because of a serialization error,
            # so we simply convert it to str if it cannot be serialized.
            obj = str(obj)
        return obj

    def _get_current_trace(self):
        trace_id = self._current_trace_id.get()
        if not trace_id:
            return None
        return self._id_to_trace[trace_id]

    def _push(self, trace: Trace):
        if not trace.id:
            trace.id = str(uuid.uuid4())
        if trace.inputs:
            trace.inputs = self.to_serializable(trace.inputs)
        trace.children = []
        if not trace.start_time:
            trace.start_time = datetime.utcnow().timestamp()
        parent_trace = self._get_current_trace()
        if not parent_trace:
            self._traces.append(trace)
            trace.node_name = self._node_name
        else:
            parent_trace.children.append(trace)
            trace.parent_id = parent_trace.id
        self._current_trace_id.set(trace.id)
        self._id_to_trace[trace.id] = trace

    @classmethod
    def pop(cls, output=None, error: Optional[Exception] = None):
        obj = cls.active_instance()
        return obj._pop(output, error) if obj else output

    def _pop(self, output=None, error: Optional[Exception] = None):
        last_trace = self._get_current_trace()
        if not last_trace:
            logging.warning("Try to pop trace but no active trace in current context.")
            return output
        if isinstance(output, Iterator):
            output = GeneratorProxy(output)
        if output is not None:
            last_trace.output = self.to_serializable(output)
        if error is not None:
            last_trace.error = self._format_error(error)
        last_trace.end_time = datetime.utcnow().timestamp()
        self._current_trace_id.set(last_trace.parent_id)

        if isinstance(output, GeneratorProxy):
            return generate_from_proxy(output)
        else:
            return output

    def to_json(self) -> list:
        return serialize(self._traces)

    @staticmethod
    def _format_error(error: Exception) -> dict:
        return {
            "message": str(error),
            "type": type(error).__qualname__,
        }


def _create_trace_from_function_call(
    f, *, args=None, kwargs=None, args_to_ignore: Optional[List[str]] = None, trace_type=TraceType.FUNCTION
):
    """
    Creates a trace object from a function call.

    Args:
        f (Callable): The function to be traced.
        args (list, optional): The positional arguments to the function. Defaults to None.
        kwargs (dict, optional): The keyword arguments to the function. Defaults to None.
        args_to_ignore (Optional[List[str]], optional): A list of argument names to be ignored in the trace.
                                                        Defaults to None.
        trace_type (TraceType, optional): The type of the trace. Defaults to TraceType.FUNCTION.

    Returns:
        Trace: The created trace object.
    """
    args = args or []
    kwargs = kwargs or {}
    args_to_ignore = set(args_to_ignore or [])
    sig = inspect.signature(f).parameters

    all_kwargs = {**{k: v for k, v in zip(sig.keys(), args)}, **kwargs}
    all_kwargs = {
        k: ConnectionType.serialize_conn(v) if ConnectionType.is_connection_value(v) else v
        for k, v in all_kwargs.items()
    }
    # TODO: put parameters in self to inputs for builtin tools
    all_kwargs.pop("self", None)
    for key in args_to_ignore:
        all_kwargs.pop(key, None)

    name = f.__qualname__
    if trace_type in [TraceType.LLM, TraceType.EMBEDDING] and f.__module__:
        name = f"{f.__module__}.{name}"

    return Trace(
        name=name,
        type=trace_type,
        start_time=datetime.utcnow().timestamp(),
        inputs=all_kwargs,
        children=[],
    )


def get_node_name_from_context():
    tracer = Tracer.active_instance()
    if tracer is not None:
        return tracer._node_name
    return None
