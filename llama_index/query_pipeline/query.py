"""Query Pipeline."""

import json
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union, cast

import networkx

from llama_index.bridge.pydantic import Field
from llama_index.callbacks import CallbackManager
from llama_index.callbacks.schema import CBEventType, EventPayload
from llama_index.core.query_pipeline.query_component import (
    ChainableMixin,
    InputKeys,
    OutputKeys,
    QueryComponent,
)
from llama_index.utils import print_text

# accept both QueryComponent and ChainableMixin as inputs to query pipeline
# ChainableMixin modules will be converted to components via `as_query_component`
QUERY_COMPONENT_TYPE = Union[QueryComponent, ChainableMixin]


def add_output_to_module_inputs(
    src_key: str,
    dest_key: str,
    output_dict: Dict[str, Any],
    module: QueryComponent,
    module_inputs: Dict[str, Any],
) -> None:
    """Add input to module deps inputs."""
    # get relevant output from link
    if src_key is None:
        # ensure that output_dict only has one key
        if len(output_dict) != 1:
            raise ValueError("Output dict must have exactly one key.")
        output = next(iter(output_dict.values()))
    else:
        output = output_dict[src_key]

    # now attach output to relevant input key for module
    if dest_key is None:
        free_keys = module.free_req_input_keys
        # ensure that there is only one remaining key given partials
        if len(free_keys) != 1:
            raise ValueError(
                "Module input keys must have exactly one key if "
                "dest_key is not specified. Remaining keys: "
                f"in module: {free_keys}"
            )
        module_inputs[next(iter(free_keys))] = output
    else:
        module_inputs[dest_key] = output


def print_debug_input(
    module_key: str,
    input: Dict[str, Any],
    val_str_len: int = 200,
) -> None:
    """Print debug input."""
    output = f"> Running module {module_key} with input: \n"
    for key, value in input.items():
        # stringify and truncate output
        val_str = (
            str(value)[:val_str_len] + "..."
            if len(str(value)) > val_str_len
            else str(value)
        )
        output += f"{key}: {val_str}\n"

    print_text(output + "\n", color="llama_lavender")


class QueryPipeline(QueryComponent):
    """A query pipeline that can allow arbitrary chaining of different modules.

    A pipeline itself is a query component, and can be used as a module in another pipeline.

    """

    callback_manager: CallbackManager = Field(
        default_factory=lambda: CallbackManager([]), exclude=True
    )

    module_dict: Dict[str, QueryComponent] = Field(
        default_factory=dict, description="The modules in the pipeline."
    )
    dag: networkx.MultiDiGraph = Field(
        default_factory=networkx.MultiDiGraph, description="The DAG of the pipeline."
    )
    verbose: bool = Field(
        default=False, description="Whether to print intermediate steps."
    )

    class Config:
        arbitrary_types_allowed = True

    def __init__(
        self,
        callback_manager: Optional[CallbackManager] = None,
        chain: Optional[Sequence[QUERY_COMPONENT_TYPE]] = None,
        **kwargs: Any,
    ):
        super().__init__(
            callback_manager=callback_manager or CallbackManager([]),
            **kwargs,
        )

        if chain is not None:
            # generate implicit link between each item, add
            self.add_chain(chain)

    def add_chain(self, chain: Sequence[QUERY_COMPONENT_TYPE]) -> None:
        """Add a chain of modules to the pipeline.

        This is a special form of pipeline that is purely sequential/linear.
        This allows a more concise way of specifying a pipeline.

        """
        # first add all modules
        module_keys = []
        for module in chain:
            module_key = str(uuid.uuid4())
            self.add(module_key, module)
            module_keys.append(module_key)

        # then add all links
        for i in range(len(chain) - 1):
            self.add_link(src=module_keys[i], dest=module_keys[i + 1])

    def add_modules(self, module_dict: Dict[str, QUERY_COMPONENT_TYPE]) -> None:
        """Add modules to the pipeline."""
        for module_key, module in module_dict.items():
            self.add(module_key, module)

    def add(self, module_key: str, module: QUERY_COMPONENT_TYPE) -> None:
        """Add a module to the pipeline."""
        # if already exists, raise error
        if module_key in self.module_dict:
            raise ValueError(f"Module {module_key} already exists in pipeline.")

        if isinstance(module, ChainableMixin):
            module = module.as_query_component()
        else:
            pass

        self.module_dict[module_key] = cast(QueryComponent, module)
        self.dag.add_node(module_key)

    def add_link(
        self,
        src: str,
        dest: str,
        src_key: Optional[str] = None,
        dest_key: Optional[str] = None,
    ) -> None:
        """Add a link between two modules."""
        if src not in self.module_dict:
            raise ValueError(f"Module {src} does not exist in pipeline.")
        self.dag.add_edge(src, dest, src_key=src_key, dest_key=dest_key)

    def _get_root_keys(self) -> List[str]:
        """Get root keys."""
        return [v for v, d in self.dag.in_degree() if d == 0]

    def _get_leaf_keys(self) -> List[str]:
        """Get leaf keys."""
        # get all modules without downstream dependencies
        return [v for v, d in self.dag.out_degree() if d == 0]

    def set_callback_manager(self, callback_manager: CallbackManager) -> None:
        """Set callback manager."""
        # go through every module in module dict and set callback manager
        self.callback_manager = callback_manager
        for module in self.module_dict.values():
            module.set_callback_manager(callback_manager)

    def run(
        self,
        *args: Any,
        return_values_direct: bool = True,
        callback_manager: Optional[CallbackManager] = None,
        **kwargs: Any,
    ) -> Any:
        """Run the pipeline."""
        # first set callback manager
        callback_manager = callback_manager or self.callback_manager
        self.set_callback_manager(callback_manager)
        with self.callback_manager.as_trace("query"):
            with self.callback_manager.event(
                CBEventType.QUERY, payload={EventPayload.QUERY_STR: json.dumps(kwargs)}
            ) as query_event:
                return self._run(
                    *args, return_values_direct=return_values_direct, **kwargs
                )

    def run_multi(
        self,
        module_input_dict: Dict[str, Any],
        callback_manager: Optional[CallbackManager] = None,
    ) -> Dict[str, Any]:
        """Run the pipeline for multiple roots."""
        callback_manager = callback_manager or self.callback_manager
        self.set_callback_manager(callback_manager)
        with self.callback_manager.as_trace("query"):
            with self.callback_manager.event(
                CBEventType.QUERY,
                payload={EventPayload.QUERY_STR: json.dumps(module_input_dict)},
            ) as query_event:
                return self._run_multi(module_input_dict)

    async def arun(
        self,
        *args: Any,
        return_values_direct: bool = True,
        callback_manager: Optional[CallbackManager] = None,
        **kwargs: Any,
    ) -> Any:
        """Run the pipeline."""
        # first set callback manager
        callback_manager = callback_manager or self.callback_manager
        self.set_callback_manager(callback_manager)
        with self.callback_manager.as_trace("query"):
            with self.callback_manager.event(
                CBEventType.QUERY, payload={EventPayload.QUERY_STR: json.dumps(kwargs)}
            ) as query_event:
                return await self._arun(
                    *args, return_values_direct=return_values_direct, **kwargs
                )

    async def arun_multi(
        self,
        module_input_dict: Dict[str, Any],
        callback_manager: Optional[CallbackManager] = None,
    ) -> Dict[str, Any]:
        """Run the pipeline for multiple roots."""
        callback_manager = callback_manager or self.callback_manager
        self.set_callback_manager(callback_manager)
        with self.callback_manager.as_trace("query"):
            with self.callback_manager.event(
                CBEventType.QUERY,
                payload={EventPayload.QUERY_STR: json.dumps(module_input_dict)},
            ) as query_event:
                return await self._arun_multi(module_input_dict)

    def _get_root_key_and_kwargs(
        self, *args: Any, **kwargs: Any
    ) -> Tuple[str, Dict[str, Any]]:
        """Get root key and kwargs.

        This is for `_run`.

        """
        ## run pipeline
        ## assume there is only one root - for multiple roots, need to specify `run_multi`
        root_keys = self._get_root_keys()
        if len(root_keys) != 1:
            raise ValueError("Only one root is supported.")
        root_key = root_keys[0]

        root_module = self.module_dict[root_key]
        if len(args) > 0:
            # if args is specified, validate. only one arg is allowed, and there can only be one free
            # input key in the module
            if len(args) > 1:
                raise ValueError("Only one arg is allowed.")
            if len(kwargs) > 0:
                raise ValueError("No kwargs allowed if args is specified.")
            if len(root_module.free_req_input_keys) != 1:
                raise ValueError("Only one free input key is allowed.")
            # set kwargs
            kwargs[next(iter(root_module.free_req_input_keys))] = args[0]
        return root_key, kwargs

    def _get_single_result_output(
        self,
        result_outputs: Dict[str, Any],
        return_values_direct: bool,
    ) -> Any:
        """Get result output from a single module.

        If output dict is a single key, return the value directly
        if return_values_direct is True.

        """
        if len(result_outputs) != 1:
            raise ValueError("Only one output is supported.")

        result_output = next(iter(result_outputs.values()))
        # return_values_direct: if True, return the value directly
        # without the key
        # if it's a dict with one key, return the value
        if (
            isinstance(result_output, dict)
            and len(result_output) == 1
            and return_values_direct
        ):
            return next(iter(result_output.values()))
        else:
            return result_output

    def _run(self, *args: Any, return_values_direct: bool = True, **kwargs: Any) -> Any:
        """Run the pipeline.

        Assume that there is a single root module and a single output module.

        For multi-input and multi-outputs, please see `run_multi`.

        """
        root_key, kwargs = self._get_root_key_and_kwargs(*args, **kwargs)
        # call run_multi with one root key
        result_outputs = self._run_multi({root_key: kwargs})
        return self._get_single_result_output(result_outputs, return_values_direct)

    async def _arun(
        self, *args: Any, return_values_direct: bool = True, **kwargs: Any
    ) -> Any:
        """Run the pipeline.

        Assume that there is a single root module and a single output module.

        For multi-input and multi-outputs, please see `run_multi`.

        """
        root_key, kwargs = self._get_root_key_and_kwargs(*args, **kwargs)
        # call run_multi with one root key
        result_outputs = await self._arun_multi({root_key: kwargs})
        return self._get_single_result_output(result_outputs, return_values_direct)

    def _validate_inputs(self, module_input_dict: Dict[str, Any]) -> None:
        root_keys = self._get_root_keys()
        # if root keys don't match up with kwargs keys, raise error
        if set(root_keys) != set(module_input_dict.keys()):
            raise ValueError(
                "Expected root keys do not match up with input keys.\n"
                f"Expected root keys: {root_keys}\n"
                f"Input keys: {module_input_dict.keys()}\n"
            )

    def _process_component_output(
        self,
        output_dict: Dict[str, Any],
        module_key: str,
        all_module_inputs: Dict[str, Dict[str, Any]],
        result_outputs: Dict[str, Any],
    ) -> None:
        """Process component output."""
        # if there's no more edges, add result to output
        if module_key in self._get_leaf_keys():
            result_outputs[module_key] = output_dict
        else:
            for _, dest, attr in self.dag.edges(module_key, data=True):
                edge_module = self.module_dict[dest]

                # add input to module_deps_inputs
                add_output_to_module_inputs(
                    attr.get("src_key"),
                    attr.get("dest_key"),
                    output_dict,
                    edge_module,
                    all_module_inputs[dest],
                )

    def _run_multi(self, module_input_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Run the pipeline for multiple roots.

        kwargs is in the form of module_dict -> input_dict
        input_dict is in the form of input_key -> input

        """
        self._validate_inputs(module_input_dict)
        queue = list(networkx.topological_sort(self.dag))

        # module_deps_inputs is a dict to collect inputs for a module
        # mapping of module_key -> dict of input_key -> input
        # initialize with blank dict for every module key
        # the input dict of each module key will be populated as the upstream modules are run
        all_module_inputs: Dict[str, Dict[str, Any]] = {
            module_key: {} for module_key in self.module_dict
        }
        result_outputs: Dict[str, Any] = {}

        # add root inputs to all_module_inputs
        for module_key, module_input in module_input_dict.items():
            all_module_inputs[module_key] = module_input

        while len(queue) > 0:
            module_key = queue.pop(0)
            module = self.module_dict[module_key]
            module_input = all_module_inputs[module_key]

            if self.verbose:
                print_debug_input(module_key, module_input)
            output_dict = module.run_component(**module_input)

            # get new nodes and is_leaf
            self._process_component_output(
                output_dict, module_key, all_module_inputs, result_outputs
            )

        return result_outputs

    async def _arun_multi(self, module_input_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Run the pipeline for multiple roots.

        kwargs is in the form of module_dict -> input_dict
        input_dict is in the form of input_key -> input

        """
        self._validate_inputs(module_input_dict)
        queue = list(networkx.topological_sort(self.dag))

        # module_deps_inputs is a dict to collect inputs for a module
        # mapping of module_key -> dict of input_key -> input
        # initialize with blank dict for every module key
        # the input dict of each module key will be populated as the upstream modules are run
        all_module_inputs: Dict[str, Dict[str, Any]] = {
            module_key: {} for module_key in self.module_dict
        }
        result_outputs: Dict[str, Any] = {}

        # add root inputs to all_module_inputs
        for module_key, module_input in module_input_dict.items():
            all_module_inputs[module_key] = module_input

        while len(queue) > 0:
            module_key = queue.pop(0)
            module = self.module_dict[module_key]
            module_input = all_module_inputs[module_key]

            if self.verbose:
                print_debug_input(module_key, module_input)
            output_dict = await module.arun_component(**module_input)

            # get new nodes and is_leaf
            self._process_component_output(
                output_dict, module_key, all_module_inputs, result_outputs
            )

        return result_outputs

    def _validate_component_inputs(self, input: Dict[str, Any]) -> Dict[str, Any]:
        """Validate component inputs during run_component."""
        return input

    def _run_component(self, **kwargs: Any) -> Dict[str, Any]:
        """Run component."""
        return self.run(return_values_direct=False, **kwargs)

    async def _arun_component(self, **kwargs: Any) -> Dict[str, Any]:
        """Run component."""
        return await self.arun(return_values_direct=False, **kwargs)

    @property
    def input_keys(self) -> InputKeys:
        """Input keys."""
        # get input key of first module
        root_keys = self._get_root_keys()
        if len(root_keys) != 1:
            raise ValueError("Only one root is supported.")
        root_module = self.module_dict[root_keys[0]]
        return root_module.input_keys

    @property
    def output_keys(self) -> OutputKeys:
        """Output keys."""
        # get output key of last module
        leaf_keys = self._get_leaf_keys()
        if len(leaf_keys) != 1:
            raise ValueError("Only one leaf is supported.")
        leaf_module = self.module_dict[leaf_keys[0]]
        return leaf_module.output_keys
