"""Dynamic plugin loader for custom NSFW scan providers."""
import importlib.util
import inspect
import logging
import os

from nsfw_scanner.providers.base import BaseProvider

logger = logging.getLogger(__name__)


def load_plugins(plugins_dir: str = "/app/plugins") -> list[BaseProvider]:
    """Load custom provider plugins from a directory.

    Each ``.py`` file in *plugins_dir* is imported dynamically.  Any class
    found that is a concrete subclass of :class:`BaseProvider` is
    instantiated and returned.

    Args:
        plugins_dir: Filesystem path to the plugins directory.

    Returns:
        A list of instantiated provider objects.
    """
    providers: list[BaseProvider] = []

    if not os.path.isdir(plugins_dir):
        logger.debug("Plugins directory not found: %s — skipping", plugins_dir)
        return providers

    for filename in sorted(os.listdir(plugins_dir)):
        if not filename.endswith(".py") or filename.startswith("_"):
            continue

        filepath = os.path.join(plugins_dir, filename)
        module_name = f"safeeye_plugin_{filename[:-3]}"

        try:
            spec = importlib.util.spec_from_file_location(module_name, filepath)
            if spec is None or spec.loader is None:
                logger.warning("Could not create module spec for %s", filepath)
                continue

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # Find all BaseProvider subclasses defined in the module
            for _name, obj in inspect.getmembers(module, inspect.isclass):
                if (
                    issubclass(obj, BaseProvider)
                    and obj is not BaseProvider
                    and not inspect.isabstract(obj)
                ):
                    instance = obj()
                    providers.append(instance)
                    logger.info(
                        "Loaded plugin provider '%s' from %s",
                        instance.name,
                        filename,
                    )

        except Exception:
            logger.exception("Failed to load plugin %s", filepath)

    return providers
