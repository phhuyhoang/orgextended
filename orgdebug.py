import sublime
import sublime_plugin
import threading
from json import dumps
from abc import abstractmethod
from collections import OrderedDict
from typing import Dict, List, Union

from OrgExtended.orgutil.sublime_utils import (
    ContextData, 
    PhantomsManager, 
    get_view_by_id
)



class OrgExtraBaseDebugCommand(sublime_plugin.WindowCommand):
    """
    Supports a JSON-view to debug the data state of any object.
    """
    def run(self, name: str = 'Debug', indent: int = 4):
        new_tab = sublime.active_window().new_file()
        new_tab.set_scratch(True)
        new_tab.set_name(name)
        new_tab.assign_syntax(sublime.find_syntax_by_scope('source.json')[0])
        self._update_view(new_tab, name, indent)

    @abstractmethod
    def get_data(self) -> Union[Dict, List]:
        """
        This function should return debug data in dict or list format
        """
        return []

    def _update_view(self, new_tab: sublime.View, name: str, indent: str = 4):
        json = OrderedDict()
        new_tab.set_read_only(False)
        json.update(self.get_data() or dict())
        new_tab.run_command('insert_content', 
            args = {
                'content': dumps(
                    json,
                    indent = indent,
                    default = lambda o: str(o)
                )
            }
        )
        new_tab.set_read_only(True)



class OrgExtraDebugThreadsCommand(OrgExtraBaseDebugCommand):
    """
    An overview tab to check which threads are running. I 
    often use it to check what happened with orgimage when images 
    downloading become slow down
    """
    def run(self):
        super().run('Debug: Threads')

    def get_data(self) -> Dict:
        debug_info = dict()
        debug_info['current_thread'] = str(threading.current_thread())
        debug_info['active_count'] = threading.active_count()
        debug_info['enumerate'] = list(map(lambda e: str(e), threading.enumerate()))
        return debug_info



class OrgExtraDebugPhantomsCommand(OrgExtraBaseDebugCommand):
    """
    Ensuring orgimage renders correctly and timely is challenging without 
    this command.
    """
    def run(self):
        super().run('Debug: Phantoms')

    def get_data(self) -> Dict:
        debug_info = OrderedDict()
        debug_info['views'] = dict()
        debug_info['histories'] = dict()
        views = debug_info['views']
        histories = debug_info['histories']
        for view_id in PhantomsManager.hub:
            view = get_view_by_id(view_id)
            if not PhantomsManager.available_for(view):
                continue
            pm = PhantomsManager.of(view)
            views[view_id] = OrderedDict()
            views[view_id]['filename'] = view.file_name()
            views[view_id]['syntax'] = view.syntax().scope
            views[view_id]['pids'] = []
            for pid in pm.pids:
                regions = pm.get_region_by_pid(pid) or [sublime.Region(-1, 1)]
                views[view_id]['pids'].append({
                    'pid': pid,
                    'url': view.substr(regions[0]),
                    'line': view.rowcol(regions[0].a)[0] + 1,
                    'keys': pm.key_refs[pid],
                    'data': pm.data_refs[pid]
                })
            histories[view_id] = [sublime.encode_value(h) for h in pm.change_histories]
        return debug_info



class OrgExtraDebugContextDataCommand(OrgExtraBaseDebugCommand):
    """
    A command for detecting memory leaks caused by ContextData when I 
    make mistakes   
    """
    def run(self):
        super().run('Debug: Context Data')

    def get_data(self) -> Dict:
        debug_info = dict()
        for view_id in ContextData.hub:
            view = get_view_by_id(view_id)
            context_data = ContextData.hub.get(view_id, {}).copy() or {}
            if view:
                context_data['kind'] = 'View'
                context_data['filename'] = view.file_name()
                context_data['syntax'] = view.syntax().scope
            else:
                context_data['kind'] = 'Panel'
            debug_info[view_id] = context_data
        return debug_info
    