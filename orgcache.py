import sublime
import sublime_plugin
from sys import getsizeof
from OrgExtended.orgutil.util import sizeof_fmt
from OrgExtended.orgutil.cache import ConstrainedCache
from OrgExtended.orgutil.sublime_utils import show_message


class OrgExtraClearCacheCommand(sublime_plugin.WindowCommand):
    """
    This command helps you proactively clear the cache
    """
    def run(self, target: str = '') -> None:
        if not ConstrainedCache.can_access(target):
            return None
        cache = ConstrainedCache.use(target)
        total_size = sizeof_fmt(cache.size())
        nouns = 'items' if len(cache.cache.keys()) > 1 else 'item'
        self.window.status_message(
            '[{} Cache] ✔️ Cleared a total of {} {}. Reclaiming {} of memory space.'.format(
                target.title(),
                len(cache.cache.keys()),
                nouns,
                total_size
            )
        )
        cache.autofreeup(True)
        sublime.set_timeout(lambda: self.window.status_message(''))


class OrgExtraDebugCacheCommand(sublime_plugin.WindowCommand):
    """
    Display debug information about what is being cached.

    Including:
    * ``"total_size"``
    * ``"count"``
    * ``"data.url"``
    * ``"data.type"``
    * ``"data.size"``
    """
    def run(self, target: str = '') -> None:
        if not ConstrainedCache.can_access(target):
            return None
        cache = ConstrainedCache.use(target)
        debug_info = dict()
        debug_info['total_size'] = sizeof_fmt(cache.size())
        debug_info['data'] = []
        for key in cache.cache.keys():
            data = cache.get(key)
            item = {}
            item['url'] = key
            item['type'] = type(data).__name__
            item['size'] = len(data) if hasattr(data, '__len__') else getsizeof(data) or 0
            debug_info['data'].append(item)
        debug_info['count'] = len(debug_info['data'])
        new_tab = sublime.active_window().new_file()
        json = sublime.encode_value(debug_info, pretty = True)
        new_tab.set_scratch(True)
        new_tab.set_name('Debug {} Cache'.format(target.title()))
        new_tab.assign_syntax(sublime.find_syntax_by_scope('source.json')[0])
        new_tab.run_command('insert_content', { 'content': json })
