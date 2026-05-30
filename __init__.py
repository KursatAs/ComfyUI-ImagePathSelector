import os

from .nodes.ImagePathSelectorNode import NODE_CLASS_MAPPINGS
from .nodes.ImagePathSelectorNode import NODE_DISPLAY_NAME_MAPPINGS

WEB_DIRECTORY = os.path.join(os.path.dirname(__file__), "web")


__all__ = ['NODE_CLASS_MAPPINGS', 'NODE_DISPLAY_NAME_MAPPINGS', 'WEB_DIRECTORY']
