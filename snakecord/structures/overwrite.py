from .base import BaseObject
from ..utils import JsonField


class PermissionOverwrite(BaseObject):
    __json_fields__ = {
        'allow': JsonField('allow', int, str),
        'deny': JsonField('deny', int, str),
        'type': JsonField('type'),
    }
