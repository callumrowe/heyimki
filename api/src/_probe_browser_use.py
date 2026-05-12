"""
Browser-use LLM introspection probe.
Exposed as a FastAPI endpoint at GET /probe-browser-use (internal only).
Returns a JSON dict with all findings.
"""
import pkgutil
import inspect


def run_probe() -> dict:
    results = {}

    # Step 1: browser_use.llm submodules
    try:
        import browser_use.llm as _llm_pkg
        submodules = [m.name for m in pkgutil.iter_modules(_llm_pkg.__path__)]
        results["llm_submodules"] = submodules
        results["llm_file"] = getattr(_llm_pkg, "__file__", "N/A")
    except Exception as e:
        results["llm_submodules_error"] = str(e)
        submodules = []

    # Step 2: Scan each submodule for classes (and their sub-submodules)
    module_classes = {}
    for modname in submodules:
        try:
            mod = __import__(f"browser_use.llm.{modname}", fromlist=["*"])
            public_names = [n for n in dir(mod) if not n.startswith("_")]
            classes = {}
            for n in public_names:
                obj = getattr(mod, n)
                if inspect.isclass(obj):
                    try:
                        sig = inspect.signature(obj.__init__)
                        params = list(sig.parameters)
                        mro = [c.__name__ for c in obj.__mro__]
                    except Exception as se:
                        params = [f"error: {se}"]
                        mro = []
                    classes[n] = {"params": params, "mro": mro}
            # If it's a package (has __path__), walk its submodules too
            if hasattr(mod, "__path__"):
                sub_sub = {}
                for ssmi in pkgutil.iter_modules(mod.__path__):
                    try:
                        ssmod = __import__(
                            f"browser_use.llm.{modname}.{ssmi.name}", fromlist=["*"]
                        )
                        ss_public = [n for n in dir(ssmod) if not n.startswith("_")]
                        ss_classes = {}
                        for n in ss_public:
                            obj = getattr(ssmod, n)
                            if inspect.isclass(obj):
                                try:
                                    sig = inspect.signature(obj.__init__)
                                    params = list(sig.parameters)
                                    mro = [c.__name__ for c in obj.__mro__]
                                except Exception as se:
                                    params = [f"error: {se}"]
                                    mro = []
                                ss_classes[n] = {"params": params, "mro": mro}
                        sub_sub[ssmi.name] = {
                            "public_names": ss_public,
                            "classes": ss_classes,
                        }
                    except Exception as sse:
                        sub_sub[ssmi.name] = {"error": str(sse)}
                module_classes[modname] = {
                    "public_names": public_names,
                    "classes": classes,
                    "is_package": True,
                    "submodules": sub_sub,
                }
            else:
                module_classes[modname] = {
                    "public_names": public_names,
                    "classes": classes,
                }
        except Exception as e:
            module_classes[modname] = {"error": str(e)}
    results["llm_modules"] = module_classes

    # Step 3: browser_use.llm top-level exports
    try:
        import browser_use.llm as _llm_pkg
        public = [x for x in dir(_llm_pkg) if not x.startswith("_")]
        top_classes = {}
        for name in public:
            obj = getattr(_llm_pkg, name)
            if inspect.isclass(obj):
                try:
                    sig = inspect.signature(obj.__init__)
                    params = list(sig.parameters)
                    mro = [c.__name__ for c in obj.__mro__]
                except Exception:
                    params = []
                    mro = []
                top_classes[name] = {"params": params, "mro": mro}
        results["llm_top_level"] = {"public_names": public, "classes": top_classes}
    except Exception as e:
        results["llm_top_level_error"] = str(e)

    # Step 4: langchain_anthropic
    try:
        from langchain_anthropic import ChatAnthropic
        sig = inspect.signature(ChatAnthropic.__init__)
        params = list(sig.parameters)
        mro = [c.__name__ for c in ChatAnthropic.__mro__]
        results["langchain_anthropic"] = {"available": True, "params": params, "mro": mro}
    except ImportError:
        results["langchain_anthropic"] = {"available": False}
    except Exception as e:
        results["langchain_anthropic"] = {"error": str(e)}

    # Step 5: browser_use.Agent params
    try:
        from browser_use import Agent
        sig = inspect.signature(Agent.__init__)
        params = list(sig.parameters)
        annotations = {}
        for pname, param in sig.parameters.items():
            if param.annotation != inspect.Parameter.empty:
                annotations[pname] = str(param.annotation)
        results["agent_params"] = params
        results["agent_annotations"] = annotations
    except Exception as e:
        results["agent_params_error"] = str(e)

    # Step 6: Try instantiating ChatAnthropic
    try:
        from langchain_anthropic import ChatAnthropic
        llm = ChatAnthropic(model="claude-sonnet-4-5", anthropic_api_key="test-key")  # type: ignore
        results["chatanthropic_instantiate"] = f"OK: {type(llm).__name__}"
    except Exception as e:
        results["chatanthropic_instantiate_error"] = str(e)

    # Step 7: Check BrowserSession constructor params
    try:
        from browser_use.browser.session import BrowserSession
        sig = inspect.signature(BrowserSession.__init__)
        params = list(sig.parameters)
        results["browser_session_params"] = params
    except Exception as e:
        results["browser_session_params_error"] = str(e)

    # Step 8: Check browser_use version
    try:
        import importlib.metadata
        ver = importlib.metadata.version("browser-use")
        results["browser_use_version"] = ver
    except Exception as e:
        results["browser_use_version_error"] = str(e)

    return results
