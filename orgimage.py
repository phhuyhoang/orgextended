"""
     ┌─────┐                                    
     │START│                                     
     └──┬──┘                                    
   _____▽______                                 
  ╱            ╲    ┌──────┐                    
 ╱ IMAGE BINARY ╲___│RENDER│                    
 ╲ IN CACHE?    ╱yes└──────┘                    
  ╲____________╱                                
        │no                                     
  ______▽______                                 
 ╱             ╲    ┌──────────────┐            
╱ CAN DOWNLOAD? ╲___│STORE TO CACHE│            
╲               ╱yes└───────┬──────┘            
 ╲_____________╱       _____▽______             
        │no           ╱            ╲    ┌──────┐
        │            ╱ IMAGE BINARY ╲___│RENDER│ 
        │            ╲ IN CACHE?    ╱yes└──────┘
        │             ╲____________╱            
        │                   │no                 
        └────┬──────────────┘                   
          ┌──▽─┐                                
          │STOP│                                
          └────┘     
"""

import re
import traceback
import sublime
import sublime_plugin
import threading
import urllib.parse
import urllib.request
import OrgExtended.orgdb as db
import OrgExtended.asettings as settings
from os import path
from uuid import uuid4
from timeit import default_timer
from sublime import Region, View
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
from OrgExtended.pymitter import EventEmitter
from OrgExtended.orgparse.startup import Startup as StartupValue
from OrgExtended.orgutil.cache import ConstrainedCache
from OrgExtended.orgutil.events import EventLoop, EventSymbol
from OrgExtended.orgutil.image import (
    Height, 
    Width, 
    image_format_of, 
    image_size_of,
    image_to_base64, 
    image_to_string,
)
from OrgExtended.orgutil.sublime_utils import (
    ContextData,
    PhantomsManager,
    SublimeStatusIndicator,
    convert_length_to_px,
    find_by_selectors,
    find_by_selector_in_region,
    get_cursor_region,
    get_syntax_by_scope,
    get_view_by_id,
    lines_from_region,
    move_to_region, 
    region_between,
    set_temporary_status, 
    show_message,
    slice_regions,
    starmap_async,
)
from OrgExtended.orgutil.threads import starmap_pools
from OrgExtended.orgutil.typecompat import Literal, Point, TypedDict
from OrgExtended.orgutil.util import (
    at, 
    download_binary, 
    is_iterable, 
    safe_call, 
    shallow_flatten, 
    split_into_chunks
)

DEFAULT_POOL_SIZE = 10
DEFAULT_CACHE_SIZE = 100 * 1024 * 1024      # 100 MB
MIN_CACHE_SIZE = 2 * 1024 * 1024            # 2 MB
MAX_TPM = 9999
STATUS_ID = 'orgextra_image'
THREAD_NAME = 'orgextra_image'
ON_CHANGE_KEY = 'orgextra_image'
ERROR_PANEL_NAME = 'orgextra_image_error'
SETTING_FILENAME = settings.configFilename + ".sublime-settings"

COMMAND_RENDER_IMAGES = 'org_extra_render_images'
COMMAND_SHOW_IMAGES = 'org_extra_show_images'
COMMAND_SHOW_THIS_IMAGE = 'org_extra_show_this_image'
COMMAND_HIDE_THIS_IMAGE = 'org_extra_hide_this_image'
COMMAND_HIDE_IMAGES = 'org_extra_hide_images'
COMMAND_SHOW_ERROR = 'org_extra_image_show_error'
COMMAND_JUMP_TO_ERROR = 'org_extra_image_jump_to_error'

EVENT_VIEWPORT_RESIZE = EventSymbol('viewport resize')

MSG_FETCH_IMAGES = '[{}] Fetching images in the {}...'
MSG_RENDER_IMAGES = 'Rendering...'
MSG_DONE = 'Done!'

SELECTOR_ORG_SOURCE = 'text.orgmode'
SELECTOR_ORG_LINK = 'orgmode.link'
SELECTOR_ORG_LINK_TEXT_HREF = 'orgmode.link.text.href'

SETTING_INSTANT_RENDER = 'extra.image.instantRender'
SETTING_USE_LAZYLOAD = 'extra.image.useLazyload'
SETTING_VIEWPORT_SCALE = 'extra.image.viewportScale'
SETTING_CACHE_SIZE = 'extra.image.cacheSize'
SETTING_DEFAULT_TIMEOUT = 'extra.image.defaultTimeout'
SETTING_TIMEOUT_PER_MEGABYTE = 'extra.image.timeoutPerMegabyte'

REGEX_ORG_ATTR = re.compile(r":(\w+)\s([\d\.]+)([\w%]*)")
REGEX_ORG_LINK = re.compile(r"\[\[([^\[\]]+)\]\s*(\[[^\[\]]*\])?\]")

LIST_SUPPORTED_IMAGE_EXTENSIONS = [".jpg", ".jpeg", ".png", ".gif"]
LIST_OUTSIDE_SECTION_REGION_RANGES = ['all', 'pre-section']
LIST_HEADLINE_SELECTORS = [
    'orgmode.headline',
    'orgmode.headline2',
    'orgmode.headline3',
    'orgmode.headline4',
    'orgmode.headline5',
    'orgmode.headline6',
    'orgmode.headline7',
    'orgmode.headline8',
    'orgmode.headline9',
]


# Setup
ImageCache = ConstrainedCache.use('image')
ImageCache.alloc(DEFAULT_CACHE_SIZE)
ImageCache.set_flag(ImageCache.FLAG_AUTOFREEUP_ON_SET, False)

emitter = EventEmitter()
_settings = sublime.load_settings(SETTING_FILENAME)

_settings.clear_on_change(ON_CHANGE_KEY)
_settings.add_on_change(ON_CHANGE_KEY, lambda: resize_alloc())


# Types
Action = Literal[
    'open', 
    'save', 
    'unfold', 
    'undefined'
]
CachedImage = TypedDict('CachedImage', {
    'original_url': Optional[str],
    'resolved_url': str,
    'size': Optional[str],
    'regions': List[Tuple[int, int]],
    'pids': List[int],
})
ChangeId = Tuple[int, int, int]
DLException = Tuple[str, Exception]
ErrorPanelRef = TypedDict('ErrorPanelRef', {
    'url': str,
    'error': str,
    'reason': str,
})
Event = Literal[
    'pre_close',
    'render',
    'render_error',
    'render_finish'
]
PhantomRefData = TypedDict('PhantomRefData', {
    'width': float,
    'height': float,
    'with_attr': bool,
    'is_hidden': bool,
    'is_placeholder': bool,
})
PID = int
OnDataHandler = Callable[['CachedImage', List['CachedImage']], None]
OnErrorHandler = Callable[[str, Exception], None]
OnFinishHandler = Callable[[List['CachedImage'], float], None]
RegionRange = Literal['folding', 'all', 'pre-section', 'auto']
RenderMethod = Literal['rescan', 'unsafe']
Startup = Literal[
    "overview",
    "content",
    "showall",
    "showeverything",
    "fold",
    "nofold",
    "noinlineimages",
    "inlineimages",
    "logdone",
    "lognotedone"
]
ViewStates = TypedDict(
    'ViewStates', 
    { 
        # Should be `True` from the moment a view gains focus.
        'initialized': bool,
        # Errors encountered while downloading images, collected for 
        # the purpose of displaying them on the output panel 
        'error_refs': Dict[str, ErrorPanelRef],
        # Indicate a view is downloading images in background. This 
        # implies that calling the command directly might be unsafe and 
        # could potentially initiate the reloading of images already in 
        # the process of being loaded.
        'is_downloading': bool,
        # Indicates the previous triggered image rendering was based on 
        # which user action. It's necessary to help skip some rendering 
        # steps to achieve instant responsiveness.
        # For example, with the 'save' action. This is a common action 
        # users take after editing ORG_ATTR, expecting to see instant 
        # changes. So, what makes us have to reload images after this 
        # action instead of just rendering what's already available?
        'prev_action': str,
        # The event loop will compare this value between two ticks to 
        # decide whether to render the images again. It helps simulate 
        # the behavior of image responsiveness, albeit with an expensive 
        # effort.
        'viewport_extent': Tuple[float, float],
    }
)
PanelStates = TypedDict(
    'PanelStates',
    {
        'view_id': int,
        'lines_refs': List[Tuple[PID, int, ErrorPanelRef]]
    }
)


def event_factory(event: Event, view: Optional[View] = None) -> str:
    """
    Returns a string that can be used as a key to emit an event for a
    specific view
    """
    suffix = '_{}'.format(view.id()) if view else ''
    return event + suffix


def cached_image(
    url: str, 
    rurl: str, 
    size: int = 0, 
    pids: List[int] = [],
    regions: List[Tuple[int, int]] = []) -> CachedImage:
    """
    Return an object can be used to transfer between commands when one 
    needs to signal to the others that the image has been loaded and 
    cached on memory.
    """
    return { 
        'original_url': url, 
        'resolved_url': rurl, 
        'size': size,
        'regions': list(regions),
        'pids': list(pids)
    }


def context_data(view: View) -> ViewStates:
    """
    Get a context wherein classes with the same view can share data 
    among themselves.
    """
    view_state = ContextData.use(view)
    # We should set the states default here
    view_state.setdefault('initialized', False)
    view_state.setdefault('error_refs', dict())
    view_state.setdefault('is_downloading', False)
    view_state.setdefault('prev_action', '')
    if 'viewport_extent' not in view_state:
        view_state['viewport_extent'] = view.viewport_extent()
    return view_state


def context_data_panel(panel: View) -> PanelStates:
    """
    Like context_data() but for panels
    """
    panel_states = ContextData.use(panel)
    panel_states.setdefault('view_id', -1)
    panel_states.setdefault('lines_refs', [])
    return panel_states


def prefixed_output_panel(name: str) -> str:
    """
    Add the prefix `output.` before the output panel name to be able to 
    open it with the `show_panel` command.
    """
    return 'output.{}'.format(name)


def startup(value: Startup) -> StartupValue:
    """
    Get a value from Startup enum in the manner of a bedridden old man
    """
    return StartupValue[value]


def collect_image_regions(view: View, region: Region) -> List[Region]:
    """
    Collect all inlineimage regions in the specified region.
    """
    href_regions = find_by_selector_in_region(view, region, SELECTOR_ORG_LINK_TEXT_HREF)
    image_regions = []
    for region in href_regions:
        url = view.substr(region)
        is_image = any(url.endswith(ext) for ext in LIST_SUPPORTED_IMAGE_EXTENSIONS)
        if is_image:
            image_regions.append(region)
    return image_regions


def detect_region_range(view: View) -> RegionRange:
    """
    Detect current region range relies on the headings around the 
    cursor
    """
    prev_, next_ = find_headings_around_cursor(view, view.sel()[0])
    if not prev_ and not next_:
        return 'all'
    elif prev_:
        return 'folding'
    else:
        return 'pre-section'


def extract_dimensions_from_attrs(
    view: View,
    textline: str,
    default_width: int = -1,
    default_height: int = -1) -> Tuple[float, float]:
    """
    Extract width and height in px from the #+ORG_ATTR line above a link.
    """
    textline = textline or ''
    viewport_scale = settings.Get(SETTING_VIEWPORT_SCALE, 1)
    try:
        attributes = re.findall(REGEX_ORG_ATTR, textline)
        width, height = default_width, default_height
        if textline.strip().startswith('#+ORG_ATTR:') and len(attributes) > 0:
            for attribute in attributes:
                key, value, unit = attribute
                if key == 'width':
                    width = convert_length_to_px(
                        view, 
                        value, 
                        unit, 
                        'width', 
                        viewport_scale)
                elif key == 'height':
                    height = convert_length_to_px(
                        view, 
                        value, 
                        unit, 
                        'height',
                        viewport_scale)
        return width, height
    except Exception as error:
        print(error)
        traceback.print_tb(error.__traceback__)
        return default_width, default_height


def fetch_or_load_image(
    url: str, 
    cwd: str, 
    timeout: Optional[float] = None,
    chunk_size: Optional[int] = None,
    termination_hook: Optional[Callable[[], bool]] = None,
    timeout_per_megabyte: Optional[Union[int, float]] = None,
) -> Optional[bytes]:
    """
    Remote fetch or load from local the image binary based on the 
    matching case of provided url.

    :param      url:      Accepts url or local file path
    :param      timeout:  Timeout for HTTP requests (None by default).
    """
    # Handle the case of the current working directory is a http/https url
    if cwd.startswith('http:') or cwd.startswith('https:'):
        relative_path = url
        absolute_url = resolve_remote(relative_path, cwd)
        response = download_binary(
            absolute_url,
            timeout,
            chunk_size,
            termination_hook,
            timeout_per_megabyte)
        return response
    # Handle the case of the provided url is already absolute
    elif url.startswith('http:') or url.startswith('https'):
        response = download_binary(
            url,
            timeout,
            chunk_size,
            termination_hook,
            timeout_per_megabyte)
        return response
    # Handle the case of the provided url is a local file path
    else:
        relative_path = url
        absolute_path = resolve_local(relative_path, cwd)
        with open(absolute_path, 'rb') as file:
            return file.read()


def find_current_headline(point: Point, hregions: List[Region]) -> Region:
    """
    Among many headings, find the heading that has the caret on it
    """
    for region in hregions:
        if region.contains(point):
            return region


def find_headings_around_cursor(
    view: View, 
    cursor: Region) -> Tuple[Optional[Region], Optional[Region]]:
    """
    Find the region of two headlines surrounding the cursor at any level.

    :returns:   A tuple containing the region of the previous and next
                headlines.
    """
    begin, end = cursor.to_tuple()
    regions = find_by_selectors(view, LIST_HEADLINE_SELECTORS)
    cursor_on_heading = any(
        view.match_selector(begin, selector) for selector in LIST_HEADLINE_SELECTORS)
    if cursor_on_heading:
        prev_ = find_current_headline(begin, regions)
        next_ = slice_regions(regions, begin = prev_.end() + 1)
        prev, next = prev_, at(next_, 0)
    else:
        prev_ = slice_regions(regions, end = end)
        next_ = slice_regions(regions, begin = begin + 1)
        prev, next = at(prev_, -1), at(next_, 0)
    return prev, next


def ignore_unchanged(
    view: View,
    regions: List[Region],
    show_hidden: bool = True) -> Tuple[List[Region], List[Region]]:
    """
    Filter out the regions that have already been rendered by 
    checking their existence in PhantomsManager. There are some 
    exceptions that will be included by the filter for re-rendering:
    - When ORG_ATTR has been added or removed.
    - When the :width or :height values of ORG_ATTR have changed.
    """
    pm = PhantomsManager.of(view)
    lines = view.lines(Region(0, view.size()))
    rendered_regions = pm.get_all_overseeing_regions()
    phantomless_regions = []
    dimension_changed_regions = []
    for region in regions:
        if region not in rendered_regions:
            phantomless_regions.append(region)
            continue

        current_line = view.line(region)
        index = lines.index(current_line)
        if index < 0:
            return dimension_changed_regions, phantomless_regions
        last_pid = pm.get_pid_by_region(region)
        last_data = pm.get_data_by_pid(last_pid) or {}
        upper_line_region = lines[index - 1]
        attr_state = with_dimension_attributes(view, upper_line_region)

        if last_data.get('is_placeholder') == True:
            phantomless_regions.append(region)
            continue

        if last_data.get('is_hidden') == True:
            if show_hidden:
                phantomless_regions.append(region)
            continue

        # Re-render if the ORG_ATTR line was added or removed
        if attr_state != last_data.get('with_attr', False):
            dimension_changed_regions.append(region)

        # Re-render if the ORG_ATTR got the image width or height change
        elif attr_state == last_data.get('with_attr') == True:
            width, height = extract_dimensions_from_attrs(
                view,
                view.substr(upper_line_region),
                default_width = last_data.get('width', -1),
                default_height = last_data.get('height', -1)
            )
            if width != last_data.get('width') or height != last_data.get('height'):
                dimension_changed_regions.append(region)

    return dimension_changed_regions, phantomless_regions


def is_folding_section(view: View) -> bool:
    """
    Return True if the cursor is positioned at a headline and the 
    content in that section headline is folding.
    """
    node = db.Get().AtInView(view)
    if node and not node.is_root():
        row, _col = view.curRowCol()
        if node.start_row == row and not node.is_folded(view):
            return True
    return False


def get_current_region(
    view: View, 
    tuple_region: Tuple[int, int],
    change_id: Optional[int] = None
) -> Region:
    """
    Retrieve the current region that has been changed based on the 
    `change_id`. Sometimes it may not be entirely reliable, so avoid 
    excessive reliance on it.
    """
    region = Region(*tuple_region)
    current_region = view.transform_region_from(region, change_id)
    if current_region.a != -1 and current_region.b != -1:
        return current_region
    current_region = view.transform_region_from(region, [0, 0, 0])
    if current_region.a != -1 and current_region.b != -1:
        return current_region
    return region


def get_cwd(view: View) -> str:
    """
    Return the current working directory (cwd) if the current view is 
    not a scratch buffer. If it is, return the cwd depending on the url 
    of the opened scratch buffer.
    """
    cwd = get_url_from_scratch(view) \
        if view.is_scratch() \
        else path.dirname(view.file_name() or '')
    return cwd


def get_url_from_scratch(view: View) -> str:
    """
    A dumb way to get the url from a buffer that is opening a 
    remote .org file
    """
    name = view.name()
    if name.strip().startswith('[org-remote]'):
        url = name.replace('[org-remote]', '').strip()
        return url
    return ''


def resize_alloc() -> None:
    """
    Adjust the image cache size according to user settings.
    """
    amount_alloc = settings.Get(SETTING_CACHE_SIZE, DEFAULT_CACHE_SIZE)
    if amount_alloc < MIN_CACHE_SIZE:
        ImageCache.alloc(MIN_CACHE_SIZE)
        return None
    if amount_alloc != ImageCache.current_max_size:
        ImageCache.alloc(int(amount_alloc))
        return None


def resolve_local(url: str, cwd: str = '/') -> str:
    """
    Convert any case of local path to absolute path, ignoring remote url.
    """
    if not url.startswith('http:') and not url.startswith('https:'):
        filepath = url
        if url.startswith("file:"):
            filepath = url.replace('file:', '')
        if filepath.startswith('/'):
            return filepath
        if filepath.startswith('~'):
            return path.expanduser(filepath)
        else:
            return path.abspath(path.join(cwd, filepath))
    return url


def resolve_remote(filepath: str, cwd: str = '') -> str:
    """
    Convert any case of a remote URL to an absolute URL. This function can be
    used to convert relative links in a remote .org file to absolute links,
    allowing us to load images for them without worry.
    """
    if filepath.startswith('http:') or filepath.startswith('https:'):
        return filepath
    if cwd.startswith('http:') or cwd.startswith('https:'):
        if filepath.startswith('file:'):
            filepath = filepath.replace('file:', '')
        if filepath.startswith('~'):
            filepath = filepath[1:]
        return urllib.parse.urljoin(cwd, filepath)
    return ''


def select_region(view: View, render_range: RegionRange) -> Region:
    """
    Select the appropriate region based on the given range 
    (folding or all)
    """
    if render_range == 'pre-section':
        prev_, next_ = find_headings_around_cursor(view, Region(0, 0))
        return select_region('all') if not next_ else region_between(view, Region(0, 0), next_)
    if render_range == 'folding':
        cursor_region = get_cursor_region(view)
        prev_, next_ = find_headings_around_cursor(view, cursor_region)
        return region_between(view, prev_, next_)
    return Region(0, view.size())


def with_dimension_attributes(view: View, region: Region) -> bool:
    """
    Return True if a region line contains an ORG_ATTR comment
    with value. In other words, if the line is just `#+ORG_ATTR:`,
    return False as usual.
    """
    substr = view.substr(region)
    width, height = extract_dimensions_from_attrs(view, substr)
    return width > 0 or height > 0


def matching_context(view: View) -> bool:
    """
    Run this function on top of the method to filter out most of 
    unappropriate scope.
    """
    if not view.match_selector(0, SELECTOR_ORG_SOURCE):
        return False
    return True


class OrgExtraImage(sublime_plugin.EventListener):
    """
    Event handlers
    """
    def on_init(self, views) -> None:
        """
        Adjust the image cache size according to user settings.
        """
        resize_alloc()


    def on_activated(self, view: View) -> None:
        """
        Reworked the show images on startup feature with performance
        optimization. It applies to the files opened from Goto Anything
        as well.
        Why not on_load?
        1. It won't solve the Goto Anything case
        2. Sometimes it would trigger the command twice
        """
        self.initialize(view, 'open')
        self.autoload(view)


    def on_post_text_command(self, view: View, command: str, args: Dict) -> Any:
        """
        Show images upon unfolding a section.
        """
        if command == 'org_tab_cycling':
            lazyload_images = settings.Get(SETTING_USE_LAZYLOAD, False)
            if not lazyload_images:
                return None
            status_message = view.get_status(STATUS_ID)
            if status_message:
                return None
            if is_folding_section(view):
                view.run_command(COMMAND_SHOW_IMAGES, { 'region_range': 'folding' })
                self.set_action(view, 'unfold')


    def on_post_save(self, view: View) -> Any:
        """
        Instantly re-render images upon saving if any ORG_ATTR values 
        associated with them are changed.
        """
        if not matching_context(view):
            return None
        if not PhantomsManager.is_being_managed(view):
            return None
        current_status = view.get_status(STATUS_ID)
        dimension_changed = self.collect_dimension_changed(
            view,
            show_hidden = False)

        if len(dimension_changed) > 0:
            def render_error_handler(value: Any, error: Exception):
                if value == listener_id:
                    emitter.off(render_error, render_error_handler)
                    render_finish_handler(value)

            def render_finish_handler(value: Any):
                if value == listener_id:
                    view.set_read_only(False)
                    emitter.off(render_finish, render_finish_handler)

            region_range = detect_region_range(view)
            selected_region = select_region(view, region_range)
            image_regions = dimension_changed
            cached_images = []
            listener_id = uuid4().hex
            cwd = get_cwd(view)
            render_error = event_factory('render_error', view)
            render_finish = event_factory('render_finish', view)
            for region in image_regions:
                url = view.substr(region)
                rurl = resolve_local(url, cwd)
                ci = cached_image(
                    url, 
                    rurl,
                    regions = [region.to_tuple()])
                cached_images.append(ci)
            view.set_read_only(True)
            emitter.once(render_finish, render_finish_handler)
            view.run_command(COMMAND_RENDER_IMAGES, 
                args = {
                    'region': selected_region.to_tuple(),
                    'images': cached_images,
                    'emit_args': listener_id,
                    'rescan': False,
                    'async_rendering': True,
                }
            )
            self.set_action(view, 'save')
            return None

        if not current_status:
            view.run_command(COMMAND_SHOW_IMAGES,
                args = {
                    'region_range': 'auto',
                    'no_download': True,
                    'show_hidden': False,
                }
            )
            self.set_action(view, 'save')


    def on_pre_close(self, view: sublime.View):
        """
        Should remove relevant data before closing out the view to
        avoid memory leaks.
        """
        if matching_context(view):
            pre_close = event_factory('pre_close', view)
            emitter.emit(pre_close)
        PhantomsManager.remove(view)
        ContextData.remove(view)


    @classmethod
    def initialize(cls, view: View, default_action: Action = 'open') -> Any:
        """
        Define some initial properties when a file open
        """
        if not matching_context(view):
            return None
        view_states = context_data(view)
        if not view_states.get('initialized') == True:
            view_states['initialized'] = True
            view_states['prev_action'] = default_action


    def autoload(self, view: View) -> None:
        """
        Display all images in the view. This action should only be 
        performed once each time the file is opened with the 'inlineimages' 
        startup in-buffer setting.
        """
        if not matching_context(view):
            return None
        # If the images have rendered before, it means its phantoms have 
        # been overseeing by the PhantomsManager as well. We should not 
        # do it again.
        if PhantomsManager.is_being_managed(view):
            return None
        try:
            inbuffer_startup = self.get_inbuffer_startup(view)
            self.kickstart_phantom_manager(view)
            if startup('inlineimages') in inbuffer_startup:
                view.run_command(COMMAND_SHOW_IMAGES, { 'region_range': 'all' })
        except Exception as error:
            print(error)
            traceback.print_tb(error.__traceback__)


    @staticmethod
    def collect_dimension_changed(view: View, show_hidden: bool = False) -> List[Region]:
        """
        Collect all cached images that have changed dimensions due to 
        adjustments in ORG_ATTR
        """
        region_range = detect_region_range(view)
        selected_region = select_region(view, region_range)
        image_regions = collect_image_regions(view, selected_region)
        dimension_changed, _ = ignore_unchanged(
            view,
            image_regions,
            show_hidden)
        return dimension_changed


    @staticmethod
    def get_inbuffer_startup(view: View) -> List[str]:
        """
        Get the in-buffer startup values place on the top of the document.
        """
        file = db.Get().FindInfo(view)
        setting_startup = settings.Get('startup', ['showall'])
        inbuffer_startup = file.org[0].startup(setting_startup)
        return inbuffer_startup


    @staticmethod
    def get_previous_action(view: View) -> Action:
        """
        Indicates the previous triggered image rendering was based on 
        which user action. It's necessary to help skip some rendering 
        steps to achieve instant responsiveness.
        For example, with the 'save' action. This is a common action 
        users take after editing ORG_ATTR, expecting to see instant 
        changes. So, what makes us have to reload images after this 
        action instead of just rendering what's already available?
        """
        view_state = context_data(view)
        return view_state['prev_action']


    @staticmethod
    def kickstart_phantom_manager(view: View) -> PhantomsManager:
        """
        Initialize Phantom Manager for the corresponding view.
        """
        PhantomsManager.adopt(view)


    @staticmethod
    def set_action(view: View, action: Action) -> 'OrgExtraImage':
        """
        Report which action has just been executed.
        """
        view_state = context_data(view)
        view_state['prev_action'] = action



class ErrorPanel(sublime_plugin.EventListener):
    """
    Handle events for the error output panel.
    """
    def on_selection_modified(self, view: View):
        """
        Jump to the region of the corresponding error line when the 
        user clicks within the error output panel.
        """
        window = view.window()
        if not window:
            return None
        output_panel = view.window().find_output_panel(ERROR_PANEL_NAME)
        if not output_panel:
            return None
        if view == output_panel:
            sel = view.sel()
            row, _ = view.rowcol(sel[0].a)
            view.window().run_command(COMMAND_JUMP_TO_ERROR, { 'index': row })



class OrgExtraShowImagesCommand(sublime_plugin.TextCommand):
    """
    An improved version of the legacy org_show_images, with the ability 
    to load images in independent threads (non-blocking) and 
    resource-sharing capabilities.
    Supports rendering range options:\n
    - 'folding': Loads and renders all images in the folding content. 
      (Default option)\n
    - 'all': Loads and renders all images in the entire document. 
      (Automatically applied to files with the setting #+STARTUP: inlineimages)
    - 'pre-section': Applies to the region preceding the first heading 
      in the document.
    - 'auto': Automatically detects the rendering range.
    """
    def run(
        self, 
        edit, 
        region_range: RegionRange = 'folding',
        no_download: bool = False,
        show_hidden: bool = True,
        image_regions: List[Tuple[int, int]] = [],
    ):
        view = self.view
        try:
            if not matching_context(view):
                return None

            # Manually check and release the image cache to ensure
            # there are enough memory space
            ImageCache.autofreeup()
            
            if region_range == 'auto':
                region_range = detect_region_range(view)

            selected_region = select_region(view, region_range)
            if not image_regions or (type(image_regions) is not list):
                image_regions = collect_image_regions(view, selected_region)
            else:
                image_regions = list(
                    map(
                        lambda image_region: Region(*image_region),
                        image_regions,
                    )
                )
            dimension_changed, phantomless = ignore_unchanged(
                view,
                image_regions,
                show_hidden)
            image_regions = dimension_changed + phantomless

            # Load images in a scratch buffer view (remote .org file) in
            # a different manner.
            cwd = get_cwd(view)

            # Show a status message: Nothing to render.
            if not image_regions:
                return self.handle_nothing_changed(status_duration = 3)

            urls = self.collect_image_urls(image_regions)
            cached_urls, noncached_urls = self.prioritize_cached(urls, cwd)

            if no_download:
                return self.handle_direct_rendering(
                    region = selected_region,
                    urls = urls,
                    cwd = cwd,
                )
            if len(phantomless):
                self.create_placeholder_phantoms(phantomless)

            exc_list = []
            listener_id = uuid4().hex
            measurement = TimeMeasurement()
            instant_render = settings.Get(SETTING_INSTANT_RENDER, False)
            fetch_status = self.use_fetch_status_indicator(region_range, len(urls))
            render_status = self.use_render_status_indicator(
                listener_id, 
                measurement,
                on_finish = lambda: self.handle_fetch_exceptions(
                    selected_region,
                    exc_list,
                ) if not instant_render else None
            )

            fetch_status.start()
            measurement.start_fetch()

            if cached_urls:
                def cache_post_finish_handler(_, timecost: float):
                    if not noncached_urls:
                        fetch_status.stop(timecost)
                        render_status.start()
                        measurement.start_render()

                # The cached images should ignore the extra.image.instantRender 
                # setting; otherwise, the main thread will be blocked during file 
                # rescanning due to multiple render command callbacks. This is the 
                # cause of the slowness when rendering so much images in a 
                # large-sized file.
                safe_call(
                    lambda: self.parallel_requests_using_threads(
                        pools = split_into_chunks(list(cached_urls), DEFAULT_POOL_SIZE),
                        cwd = cwd,
                        on_finish = self.on_threads_finished(
                            selected_region,
                            emit_args = listener_id,
                            then = cache_post_finish_handler
                        )
                    )
                )
                fetch_status.succeed(len(cached_urls))

            view_state = context_data(view)
            view_state['is_downloading'] = True

            timeout = settings.Get(SETTING_DEFAULT_TIMEOUT, None) or None
            tpm = settings.Get(SETTING_TIMEOUT_PER_MEGABYTE, None) or MAX_TPM

            def post_download_handler(image: CachedImage):
                fetch_status.succeed()

            def post_finish_handler(images: List[CachedImage], timecost: float):
                view_state['is_downloading'] = False
                fetch_status.stop(timecost)
                if not instant_render:
                    render_status.start()
                    measurement.start_render()
                else:
                    self.handle_fetch_exceptions(
                        selected_region,
                        exc_list)

            def exception_handler(url: str, error: Exception):
                fetch_status.failed()
                exc_list.append((url, error))

            # Download non-cached images
            sublime.set_timeout_async(
                lambda: self.parallel_requests_using_threads(
                    pools = split_into_chunks(list(noncached_urls), DEFAULT_POOL_SIZE),
                    cwd = cwd,
                    on_data = self.on_downloaded(
                        selected_region, 
                        post_download_handler,
                    ),
                    on_error = exception_handler,
                    on_finish = self.on_threads_finished(
                        selected_region, 
                        emit_args = listener_id,
                        then = post_finish_handler,
                    ),
                    timeout = timeout,
                    timeout_per_megabyte = tpm
                )
            )
                        
            # Limiting to 5 seconds for each image download.
            # If all images exceed this time threshold, the status 
            # message below will be set.
            sublime.set_timeout(
                lambda: fetch_status.is_running() \
                    and fetch_status.set('Slow internet! Be patient...'),
                len(urls) * 5 * 1000)
        except Exception as error:
            show_message(error, level = 'error')
            traceback.print_tb(error.__traceback__)


    def create_placeholder_phantoms(self, regions: Region) -> None:
        """
        Pre-create phantoms to track the regions for in-time rendering 
        instead of doing so afterward. This enhances the user experience 
        and enables file editing while waiting for images to load.
        """
        pm = PhantomsManager.of(self.view)
        pid = None
        for image_region in regions:
            if pm.has_phantom(image_region):
                pid = pm.get_pid_by_region(image_region)
                pm.erase_phantom(pid)
            new_pid = pm.add_phantom(image_region, '', { 'is_placeholder': True })
            pm.update_history(new_pid, pid)


    def collect_image_urls(self, regions: List[Region]) -> Set[str]:
        """
        Collect URLs as strings from their respective regions, removing 
        duplicates to optimize download time.
        """
        urls = map(lambda r: self.view.substr(r), regions)
        return set(urls)


    def handle_nothing_changed(self, status_duration: int) -> None:
        """
        Only call this method with a return statement when the view has 
        nothing to update.
        
        :param      status_duration:  Delay in second to clear the status message
        """
        return set_temporary_status(self.view, STATUS_ID, 'Nothing to render.', status_duration)


    def handle_direct_rendering(
        self, 
        region: Region,
        urls: List[str], 
        cwd: str
    ) -> None:
        """
        When this method is called, ignore the download step, nothing 
        new will be cached, so the render command may reuse what has 
        already been cached.
        """
        tuple_region = region.to_tuple()
        images = list(
            map(
                lambda url: cached_image(url, resolve_local(url, cwd)), 
                urls
            )
        )
        return self.view.run_command(COMMAND_RENDER_IMAGES,
            args = {
                'region': tuple_region,
                'images': images
            }
        )


    def handle_fetch_exceptions(
        self, 
        selected_region: Region,
        exc_list: List[DLException]) -> None:
        """
        Show an output panel indicating what errors have occurred.
        """
        view_state = context_data(self.view)
        error_refs = view_state['error_refs']
        error_refs.clear()
        for exc in exc_list:
            url, exception = exc
            error_refs[url] = {
                'url': url,
                'error': exception.__class__.__name__,
                'reason': str(exception)
            }
        self.view.run_command(COMMAND_SHOW_ERROR,
            args = {
                'view_id': self.view.id(),
                'selected_region': selected_region.to_tuple(),
            }
        )


    def on_downloaded(
        self,
        region: Region,
        then: Optional[OnDataHandler] = None) -> OnDataHandler:
        """
        Implement the instant render behaviour.
        """
        pm = PhantomsManager.of(self.view)
        def on_data(cached_image: CachedImage, images: List[CachedImage]):
            instant_render = settings.Get(SETTING_INSTANT_RENDER, False)
            # Render that image and exclude it from re-rendering in the 
            # 'finish' event.
            if instant_render:
                pids = []
                cached_image['pids'] = pids
                for pid in pm.pids:
                    regions = pm.get_region_by_pid(pid)
                    if not regions:
                        continue
                    substr = self.view.substr(regions[0])
                    if substr == cached_image.get('original_url'):
                        cached_image['pids'].append(pid)
                # With pids, we may no longer need to concern ourselves 
                # with rescanning, thereby accelerating image rendering 
                # and making unsafe rendering safer.
                rescan = len(pids) == 0
                self.view.run_command(COMMAND_RENDER_IMAGES, 
                    args = {
                        'region': region.to_tuple(),
                        'images': [cached_image],
                        'rescan': rescan
                    }
                )
                images.remove(cached_image)
            if callable(then): safe_call(then, [cached_image])
        return on_data


    def on_threads_finished(
        self,
        region: Region,
        then: Optional[Callable[[int], None]] = None,
        emit_args: Any = None,
    ) -> Callable[[List[CachedImage], int], None]:
        """
        Chaining calls the render command to automatically render the 
        cached images.
        """
        def on_finish(cached_images: List[CachedImage], timecost: int):
            if callable(then):
                safe_call(then, [cached_images, timecost])
            self.view.run_command(COMMAND_RENDER_IMAGES, 
                args = {
                    'region': region.to_tuple(),
                    'images': cached_images,
                    'emit_args': emit_args,
                }
            )
        return on_finish


    def parallel_requests_using_sublime_timeout(
        self,
        pools: List[List[str]],
        cwd: str,
        on_data: Optional[OnDataHandler] = None,
        on_error: Optional[OnErrorHandler] = None,
        on_finish: Optional[OnFinishHandler] = None,
        timeout: Optional[float] = None
    ) -> None:
        """
        Asynchronously run fetching image operations, where each 
        operation takes a list of URLs/filepaths that will be loaded one 
        after another. The following are the measurement results that 
        can be compared to `.parallel_requests_using_threads()`:
        - 1 image link: 1.482095375999961, 1.335890114999529, 1.298129551000784
        - 10 image links: 8.051044771000306, 7.954113742000118, 8.047068934999515
        - 85 image links: 93.40657268199993
        I don't quite understand the mechanism behind this API, nor 
        whether I am using it correctly, but it doesn't seem 
        significantly faster compared to the synchronous approach.
        """
        start = default_timer()
        stop = event_factory('pre_close', self.view)
        flag = threading.Event()
        cached_images = []
        emitter.once(stop, lambda: flag.set())
        def async_all_finish(ci: List[CachedImage]) -> None:
            if is_iterable(ci):
                cached_images.extend(ci)
                if callable(on_finish):
                    safe_call(on_finish, [shallow_flatten(cached_images), default_timer() - start])

        starmap_async(
            callback = lambda urls: self.thread_execution(
                urls,
                cwd,
                on_data,
                on_error,
                timeout,
                flag),
            args = pools,
            on_finish = async_all_finish)


    def parallel_requests_using_threads(
        self,
        pools: List[List[str]],
        cwd: str,
        on_data: Optional[OnDataHandler] = None,
        on_error: Optional[OnErrorHandler] = None,
        on_finish: Optional[OnFinishHandler] = None,
        timeout: Optional[float] = None,
        timeout_per_megabyte: Optional[Union[int, float]] = None,
    ) -> List[CachedImage]:
        """
        Run threads of fetching image operations in parallel, where each 
        thread takes a list of urls/filepaths that will be loaded one 
        after another.
        The following are the measurement results that can be compared to 
        `.parallel_requests_using_sublime_timeout()`: \n
        * 1 image link: 1.1709886939997887, 1.2005657659992721, 1.2650837740002316 \n
        * 10 image links: 1.387642825000512, 1.5033762000002753, 2.1559584730002825 \n
        * 85 image links: 16.07192872700034, 12.695106612000018, 9.564963673999955 \n
        As you can see, it's up to ten times faster. \n
        And of course it's way more faster compared to the traditional 
        method.
        """
        start = default_timer()
        flag = threading.Event()
        pre_close = event_factory('pre_close', self.view)
        listener = lambda: flag.set()
        emitter.once(pre_close, listener)
        try:
            results = shallow_flatten(
                starmap_pools(
                    lambda urls: self.thread_execution(
                        urls, 
                        cwd, 
                        on_data, 
                        on_error, 
                        timeout,
                        flag,
                        timeout_per_megabyte),
                    pools,
                    name = THREAD_NAME
                ))
            if callable(on_finish):
                safe_call(on_finish, [results, default_timer() - start])
        except Exception as error:
            print(error)
            traceback.print_tb(error.__traceback__)
            results = []
        finally:
            emitter.clear_listeners(pre_close)
        return results


    def prioritize_cached(self, urls: List[str], cwd: str) -> Tuple[Set[str], Set[str]]:
        """
        Separate the cached images from those that should be downloaded 
        before rendering. 
        Return two sets in order: cached and non-cached.
        """
        cached, non_cached = set(), set()
        for url in urls:
            resolved_url = resolve_local(url, cwd)
            if ImageCache.has(resolved_url):
                cached.add(url)
            else:
                non_cached.add(url)
        return cached, non_cached


    def thread_execution(
        self,
        urls: List[str],
        cwd: str,
        on_data: Optional[OnDataHandler] = None,
        on_error: Optional[OnErrorHandler] = None,
        timeout: Optional[float] = None,
        stop_event: Optional[threading.Event] = None,
        tpm: Optional[Union[int, float]] = None,
    ) -> List[CachedImage]:
        """
        This method specifies what a thread should do, typically 
        revolving around ensuring that the images have been cached and 
        are ready for rendering.
        """
        loaded_images = []
        termination_hook = lambda: stop_event.is_set() \
            if isinstance(stop_event, threading.Event) \
            else None
        for url in urls:
            try:
                resolved_url = resolve_local(url, cwd)
                loaded_binary = None
                cached_binary = ImageCache.get(resolved_url)
                if type(cached_binary) is bytes:
                    loaded_binary = cached_binary
                    loaded_image = cached_image(url, resolved_url, len(loaded_binary))
                    loaded_images.append(loaded_image)
                else:
                    loaded_binary = fetch_or_load_image(
                        url, 
                        cwd, 
                        timeout,
                        termination_hook = termination_hook,
                        timeout_per_megabyte = tpm)
                    loaded_image = cached_image(url, resolved_url, len(loaded_binary or ''))
                    loaded_images.append(loaded_image)
                    if type(loaded_binary) is bytes:
                        ImageCache.set(resolved_url, loaded_binary)
                if isinstance(stop_event, threading.Event):
                    if stop_event.is_set():
                        raise ThreadTermination()
                if loaded_binary is None and callable(on_error):
                    safe_call(on_error, [
                        url, 
                        CorruptedImage('got a corrupted image')
                    ])
                elif callable(on_data):
                    safe_call(on_data, [loaded_image, loaded_images])
            except Exception as error:
                safe_call(on_error, [url, error])
                continue
        return loaded_images


    def use_fetch_status_indicator(
        self, 
        region_range: RegionRange,
        total_count: Optional[int] = None,
        update_interval: Optional[int] = 100,
    ) -> SublimeStatusIndicator:
        """
        Setup a loading indicator with messages on the status bar
        """
        mode = 'inlineimages' if region_range == 'all' else 'unfold'
        area = 'document' if region_range == 'all' else 'folding section'
        return SublimeStatusIndicator(
            self.view, 
            STATUS_ID,
            message = MSG_FETCH_IMAGES.format(mode, area),
            finish_message = MSG_RENDER_IMAGES,
            total_count = total_count,
            update_interval = update_interval)


    def use_render_status_indicator(
        self,
        listener_id: str,
        measurement: 'TimeMeasurement',
        update_interval: Optional[int] = 100,
        on_finish: Optional[Callable] = None,
    ) -> SublimeStatusIndicator:
        """
        Setup a loading indicator with messages on the status bar
        """
        render_message = '{} ({})'
        finish_message = '{} ({})'
        status = SublimeStatusIndicator(
            self.view,
            STATUS_ID,
            message = render_message.format(MSG_RENDER_IMAGES, 0),
            finish_message = finish_message.format(MSG_DONE, 0),
            update_interval = update_interval)
        counter = status.status_counter()

        render = event_factory('render', self.view)
        render_finish = event_factory('render_finish', self.view)

        def render_event_handler(value: Any, image_region: Region):
            if value == listener_id:
                counter.succeed += 1
                status.set(message = render_message.format(MSG_RENDER_IMAGES, counter.succeed))
                status.succeed(update = True)

        def render_finish_handler(value: Any):
            if value == listener_id:
                emitter.clear_listeners(render)
                emitter.clear_listeners(render_finish)
                measurement.stop_render()
                accumulated_timecost = measurement.accumulated_timecost()
                status.set(
                    finish_message = finish_message.format(
                        MSG_DONE,
                        counter.succeed
                    )
                )
                status.stop(accumulated_timecost)
                if callable(on_finish):
                    safe_call(on_finish)

        emitter.on(render, render_event_handler)
        emitter.once(render_finish, render_finish_handler)
        return status



class OrgExtraShowThisImageCommand(sublime_plugin.TextCommand):
    """
    Show a single image
    """
    def run(
        self,
        edit,
        image_region: Optional[Tuple[int, int]] = [None, None]):
        view = self.view
        try:
            if not matching_context(view):
                return None
            begin, end = image_region
            if (type(begin) is not int) or (type(end) is not int):
                sel = view.sel()
                region = view.expand_to_scope(
                    sel[0].begin(),
                    SELECTOR_ORG_LINK_TEXT_HREF)
                if region is None:
                    return None
                image_region = region.to_tuple()
            
            view_state = context_data(view)
            if not view_state['is_downloading']:
                return view.run_command(COMMAND_SHOW_IMAGES,
                    args = {
                        'region_range': 'auto',
                        'show_hidden': True,
                        'image_regions': [image_region]
                    }
                )
        except Exception as error:
            show_message(error, level = 'error')
            traceback.print_tb(error.__traceback__)



class OrgExtraHideThisImageCommand(sublime_plugin.TextCommand):
    """
    Hide the image in the region specified by `image_region`. If this 
    parameter is not provided, automatically expand to a 
    `orgmode.link.text.href` scope around the cursor if exist.
    Essentially, this command is similar to the legacy `org_hide_image` 
    command, except that it only hides images that are being managed by 
    PhantomsManager.
    Furthermore, even though those images is hidden from view, the phantoms 
    containing those images are still retained (which is why you see the 
    next line looks a little bulged, about 1-2px). This helps the 
    `show_images` command distinguish between hidden images and newly 
    added/phantomless images through the attached `is_hidden` flag.
    """
    def run(
        self, 
        edit, 
        image_region: Optional[Tuple[int, int]] = [None, None]):
        view = self.view
        try:
            if not matching_context(view):
                return None
            begin, end = image_region
            if type(begin) is int and type(end) is int:
                region = Region(begin, end)
                pm = PhantomsManager.of(view)
                pid = pm.get_pid_by_region(region)
                if pid is not None:
                    is_erased = pm.erase_phantom(pid)
                    if is_erased:
                        new_pid = pm.add_phantom(region, '', { 'is_hidden': True })
                        pm.update_history(new_pid, pid)
            else:
                sel = view.sel()
                region = view.expand_to_scope(
                    sel[0].begin(),
                    SELECTOR_ORG_LINK_TEXT_HREF)
                if region is not None:
                    view.run_command(
                        COMMAND_HIDE_THIS_IMAGE, { 'image_region': region.to_tuple() })
        except Exception as error:
            show_message(error, level = 'error')
            traceback.print_tb(error.__traceback__)



class OrgExtraHideImagesCommand(sublime_plugin.TextCommand):
    """
    Selecting and hiding images in region.
    """
    status_id = STATUS_ID + '_hide'
    status_duration_in_second = 3

    def run(
        self,
        edit,
        region_range: RegionRange = 'folding'
    ):
        view = self.view
        try:
            if not matching_context(view):
                return None

            if region_range == 'auto':
                region_range = detect_region_range(view)

            pm = PhantomsManager.of(view)
            selected_region = select_region(view, region_range)
            image_regions = collect_image_regions(view, selected_region)
            erased_count = 0
            for region in image_regions:
                pid = pm.get_pid_by_region(region)
                if pid is not None:
                    data = pm.get_data_by_pid(pid)
                    if data.get('is_hidden'):
                        continue
                    is_erased = pm.erase_phantom(pid)
                    if is_erased:
                        erased_count += 1
                        new_pid = pm.add_phantom(region, '', { 'is_hidden': True })
                        pm.update_history(new_pid, pid)

            if erased_count > 0:
                set_temporary_status(
                    view,
                    self.status_id,
                    ('Success! {} image is now hidden' \
                        if erased_count == 1 \
                        else 'Success! {} images are now hidden').format(erased_count),
                    self.status_duration_in_second)
        except Exception as error:
            show_message(error, level = 'error')
            traceback.print_tb(error.__traceback__)



class OrgExtraRenderImagesCommand(sublime_plugin.TextCommand):
    """
    Render cached image to the view with the ideal size.\n
    ---
    Firstly, you need to know that ImageCache is a ConstrainedCache with 
    keys being image URLs or absolute filepath, facilitating access to 
    their cached binary data.\n
    ---
    There are two ways to use this command:
    - With rescan: Automatically rescan the provided region to 
    search for all phantoms containing images and update those specified 
    in the images argument value. This is suitable in cases where you 
    want to allow users to edit the file while waiting for images to 
    be loaded. It's a relatively safe option, as it doesn't omit any 
    images during rendering, but it's relatively slow due to the need 
    to rescan the file.
    - Without rescan: Render images directly without re-scanning by 
    using region of each image provided in the images list. This option 
    is faster but may not be as safe, especially if there are inadvertent 
    edits to the file causing a mismatch with the specified images, 
    leading to a series of subsequent images that cannot be rendered. You 
    probably need to set the file as read-only before using the command 
    and ensure that all provided regions point to scopes matching the 
    `orgmode.link.text.href` selector to make the command work.
    """
    def run(
        self,
        edit,
        region: Optional[Tuple[int, int]] = None,
        images: List[CachedImage] = [],
        rescan: bool = True,
        emit_args: Any = None,
        async_rendering: bool = False, 
    ) -> None:
        try:
            if not matching_context(self.view) or not region:
                return None
            render = lambda: None
            if rescan:
                render = lambda: self.handle_rescan_render(
                    region, 
                    images,
                    emit_args)
            else:
                render = lambda: self.handle_unsafe_render(images, emit_args)
            if async_rendering:
                sublime.set_timeout_async(render)
            else:
                safe_call(render)
        except Exception as error:
            print(error)
            traceback.print_tb(error.__traceback__)


    def extract_image_dimensions(
        self,
        attr_line: str,
        image_binary: bytes) -> Tuple[int, int, bool]:
        """
        Extract image width, height from its binary.

        :returns: A tuple containing width, height, and a boolean value 
        indicating whether the image is resized using ORG_ATTR.
        """
        width, height = extract_dimensions_from_attrs(self.view, attr_line)
        if width > 0 and height > 0:
            return width, height, True
        else:
            b_width, b_height = image_size_of(image_binary)
            if b_width is None: b_width = 0
            if b_height is None: b_height = 0
            if width > 0:
                return width, (width * b_height / b_width), True
            elif height > 0:
                return (height * b_width / b_height), height, True
            else:
                return b_width, b_height, False


    def fit_to_viewport(
        self, 
        vw: Union[int, float], 
        width: int, 
        height: int) -> Tuple[int, int]:
        """
        Adjust the image width to ensure it will not exceed the viewport.
        Adjust the height to make it scale with width as well.
        """
        if width > vw:
            temp = vw / width
            height *= temp
            width = vw
        return width, height


    def get_image_binary(
        self,
        image: CachedImage
    ) -> Union[bytes, None]:
        """
        Attempt to retrieve the binary image by trying each entry in the 
        CachedImage.
        """
        resolved_url = image.get('resolved_url')
        original_url = image.get('original_url')
        if ImageCache.has(resolved_url):
            return ImageCache.get(resolved_url)
        else:
            return self.get_image_binary_kv('original_url', original_url)


    def get_image_binary_kv(
        self, 
        key: Literal[
            'resolved_url',
            'original_url',
            'region',
            'pid'
        ],
        value: Union[str, int],
        cwd: Optional[str] = None,
    ) -> Union[bytes, None]:
        """
        Retrieve image binary from one of the keys provided by the 
        CachedImage object.
        """
        if key == 'resolved_url':
            return ImageCache.get(value)
        elif key == 'original_url':
            cwd = cwd or get_cwd(self.view)
            return ImageCache.get(resolve_local(value, cwd))
        elif key == 'pid':
            pm = PhantomsManager.of(self.view)
            regions = pm.get_region_by_pid(value)
            if not regions:
                return None
            region = regions.pop(0)
            return ImageCache.get(self.view.substr(region))
        elif key == 'region':
            return ImageCache.get(self.view.substr(value))
        else:
            return None


    def handle_render(
        self, 
        region: Region, 
        image_bin: bytes,
        lines: List[str] = [],
        extent: Optional[Tuple[float, float]] = None,
    ) -> None:
        """
        DRY render method.
        """
        line_region = self.view.line(region)
        line_text = self.view.substr(line_region)
        index = lines.index(line_text)
        preceding_line = lines[index - 1] if index > 0 else None
        indent_level = len(line_text) - len(line_text.lstrip())
        vw, _ = self.view.viewport_extent() if not extent else extent
        width, height, is_resized = self.extract_image_dimensions(
            preceding_line,
            image_bin)
        width, height = self.fit_to_viewport(vw, width, height)
        self.render_image(
            region, 
            image_bin,
            width,
            height,
            indent_level,
            is_resized)


    def handle_rescan_render(
        self,
        tuple_region: Optional[Tuple[int, int]],
        images: List[CachedImage] = [],
        emit_args: Any = None,
    ) -> None:
        """
        Rescan the region before rendering.
        """
        render = event_factory('render', self.view)
        render_error = event_factory('render_error', self.view)
        render_finish = event_factory('render_finish', self.view)
        try:
            selected_region = Region(*tuple_region)
            lines = lines_from_region(self.view, selected_region)
            viewport_width, _ = self.view.viewport_extent()
            image_dict = self.to_resolved_url_dict(images)
            original_urls = image_dict.keys()
            href_regions = find_by_selector_in_region(
                self.view, 
                selected_region, 
                SELECTOR_ORG_LINK_TEXT_HREF)
            for region in href_regions:
                url = self.view.substr(region)
                line_region = self.view.line(region)
                line_text = self.view.substr(line_region)
                index = lines.index(line_text)
                preceding_line = lines[index - 1] if index > 0 else None
                resolved_url = image_dict.get(url)
                image_binary = ImageCache.get(resolved_url)
                indent_level = len(line_text) - len(line_text.lstrip())
                if not url in original_urls or not image_binary:
                    continue
                width, height, is_resized = self.extract_image_dimensions(
                    preceding_line,
                    image_binary)
                width, height = self.fit_to_viewport(
                    viewport_width,
                    width,
                    height)
                self.render_image(
                    region,
                    image_binary,
                    width,
                    height,
                    indent_level,
                    is_resized)
                emitter.emit(render, emit_args, region)
            emitter.emit(render_finish, emit_args)
        except Exception as error:
            emitter.emit(render_error, emit_args, error)
            raise error


    def handle_unsafe_render(
        self,
        images: List[CachedImage] = [],
        emit_args: Any = None,
    ) -> None:
        """
        Render without rescanning but requires regions where the image 
        urls are located.
        """
        render = event_factory('render', self.view)
        render_error = event_factory('render_error', self.view)
        render_finish = event_factory('render_finish', self.view)
        try:
            entire = Region(0, self.view.size())
            lines = lines_from_region(self.view, entire)
            extent = self.view.viewport_extent()
            for image in images:
                pids = image.get('pids', [])
                binary = self.get_image_binary(image)
                regions_set = set(
                    map(
                        lambda r: tuple(r), 
                        image.get('regions', [])
                    )
                )
                for pid in pids:
                    pm = PhantomsManager.of(self.view)
                    regions = pm.get_region_by_pid(pid)
                    if regions:
                        regions_set.add(regions[0].to_tuple())
                for tuple_region in regions_set:
                    region = Region(*tuple_region)
                    image_binary = binary or \
                        self.get_image_binary_kv('region', region)
                    if image_binary:
                        self.handle_render(
                            region,
                            image_binary,
                            lines,
                            extent)
                        emitter.emit(render, emit_args, region)
            emitter.emit(render_finish, emit_args)
        except Exception as error:
            emitter.emit(render_error, emit_args, error)
            raise error


    def render_image(
        self,
        image_region: Region,
        image_binary: bytes,
        image_width: Width,
        image_height: Height,
        indent_level: int = 0, 
        with_attr: bool = False) -> bool:
        """
        Render the image.
        """
        try:
            width, height = int(image_width), int(image_height)
            image_format = image_format_of(image_binary)
            space_indent = '&nbsp;' * (indent_level * 2)
            pm = PhantomsManager.of(self.view)
            pid = None
            if image_format == 'svg':
                html = image_to_string(image_binary)
            else:
                base64 = image_to_base64(image_binary)
                html = u'{}<img src="data:image/{}" class="centerImage" {}>'.format(
                    space_indent,
                    '{};base64,{}'.format(image_format, base64),
                    'width="{}" height="{}"'.format(width, height),
                )
            if pm.has_phantom(image_region):
                pid = pm.get_pid_by_region(image_region)
                pm.erase_phantom(pid)
            
            new_pid = pm.add_phantom(image_region, html, { 
                'width': width, 
                'height': height,
                'with_attr': with_attr })
            pm.update_history(new_pid, pid)
            return True
        except Exception as error:
            print(error)
            traceback.print_tb(error.__traceback__)
            return False


    def to_resolved_url_dict(self, images: List[CachedImage]) -> Dict[str, CachedImage]:
        """
        Converting a list of images into a dictionary, allowing us to 
        quickly retrieve the corresponding resolved absolute link using 
        the current relative link.
        """
        resolved_url_dict = dict()
        for image in images:
            original_url = image.get('original_url')
            if original_url is None:
                continue
            resolved_url_dict[original_url] = image.get('resolved_url')
        return resolved_url_dict



class OrgExtraImageJumpToErrorCommand(sublime_plugin.WindowCommand):
    """
    Jump to the region containing errors.
    """
    def run(self, index: int) -> None:
        try:
            panel = self.window.find_output_panel(ERROR_PANEL_NAME)
            if not panel:
                return None
            panel_state = context_data_panel(panel)
            lines_refs = panel_state.get('lines_refs')
            if index >= len(lines_refs):
                return None
            view_id = panel_state.get('view_id')
            target_view = get_view_by_id(view_id)
            if not target_view:
                return None
            pid, _ = lines_refs[index]
            pm = PhantomsManager.of(target_view)
            regions = pm.get_region_by_pid(pid)
            if not regions:
                return None
            current_region = regions[0]
            self.window.focus_view(target_view)
            move_to_region(target_view, current_region, animate = True)
        except Exception as error:
            show_message(error)
            traceback.print_tb(error.__traceback__)



class OrgExtraImageShowErrorCommand(sublime_plugin.TextCommand):
    """
    Show an output panel indicating what errors have occurred.
    """
    ERROR_LINE = '{}. [[{}]] {}: {}'

    def run(
        self,
        edit,
        view_id: int,
        selected_region: Optional[Tuple[int, int]] = None
    ):
        try:
            if not matching_context(self.view):
                return None
            target_view = get_view_by_id(view_id)
            if not matching_context(target_view):
                return None
            panel = self.get_panel(target_view)
            any_error = self.update_panel(
                edit, 
                panel, 
                target_view,
                selected_region)
            if any_error:
                self.show_panel(panel)
        except Exception as error:
            print(error)
            traceback.print_tb(error.__traceback__)


    def show_panel(self, panel: View) -> None:
        panel.show(0)
        self.view.window().run_command('show_panel',
            args = {
                'panel': prefixed_output_panel(ERROR_PANEL_NAME)
            }
        )


    def update_panel(
        self,
        edit,
        panel: View,
        view: View,
        selected_region: Optional[Tuple[int, int]] = None
    ) -> bool:
        view_state = context_data(view)
        panel_state = context_data_panel(panel)
        error_refs = view_state.get('error_refs')
        lines_refs = panel_state.get('lines_refs')

        if not error_refs:
            return None

        tuple_region = selected_region or (0, view.size())
        region = Region(*tuple_region)
        pm = PhantomsManager.of(view)
        number_of_lines = len(view.lines(Region(0, view.size())))
        pad_length = len(str(number_of_lines)) + 1
        panel_text = []
        pids_by_row = {}

        if view.id() != panel_state.get('view_id'):
            panel_state['view_id'] = view.id()

        lines_refs.clear()
        panel.set_read_only(False)
        panel.erase(edit, Region(0, panel.size()))
        # Weird bug
        [ (view.substr(r) for r in pm.get_region_by_pid(p)) for p in pm.pids ]
        for pid in pm.pids:
            regions = pm.get_region_by_pid(pid)
            if not regions:
                continue
            region = regions[0]
            url = view.substr(region)
            if url in error_refs:
                row, _ = view.rowcol(region.a)
                pids_by_row[row] = (pid, url)
        for row in sorted(pids_by_row.keys()):
            pid, url = pids_by_row[row]
            ref = error_refs[url]
            error, reason = ref.get('error'), ref.get('reason')
            line_text = self.ERROR_LINE.format(
                str(row + 1).rjust(pad_length, ' '),
                url,
                error,
                reason)
            panel_text.append(line_text)
            lines_refs.append((pid, ref))
        panel.insert(edit, 0, '\n'.join(panel_text))
        panel.set_read_only(True)
        return len(panel_text) > 0


    @classmethod
    def get_panel(cls, view: View) -> View:
        panel = view.window().find_output_panel(ERROR_PANEL_NAME)
        if not panel:
            panel = view.window().create_output_panel(ERROR_PANEL_NAME)
            cls.setup_panel(panel)
        return panel


    @classmethod
    def get_panel_syntax(cls) -> sublime.Syntax:
        syntax = get_syntax_by_scope('text.orgmode')
        if not syntax:
            syntax = get_syntax_by_scope('text.plain')
        return syntax


    @classmethod
    def setup_panel(cls, panel: View) -> None:
        settings = panel.settings()
        settings.set('caret_extra_width', 0)
        settings.set('drag_text', False)
        settings.set('fold_buttons', False)
        settings.set('highlight_line', True)
        settings.set('line_numbers', False)
        settings.set('scroll_past_end', False)
        settings.set('spell_check', False)
        settings.set('draw_indent_guides', False)
        panel.set_read_only(True)
        try:
            panel.assign_syntax('scope:{}'.format(cls.get_panel_syntax().scope))
        except Exception as error:
            print(error)
            traceback.print_tb(error.__traceback__)
            panel.assign_syntax(cls.get_panel_syntax())



class TimeMeasurement(object):
    """
    Calculate the accumulated timecost
    """
    def __init__(self) -> None:
        self.timecost = 0
        self.fetch_begin = 0
        self.fetch_end = 0
        self.render_begin = 0
        self.render_end = 0
        self.fetching = False
        self.rendering = False
        self.finished = False

    def is_fetching(self) -> bool:
        return self.fetching

    def is_rendering(self) -> bool:
        return self.rendering

    def is_lazy(self) -> bool:
        return not self.fetching and not self.rendering

    def is_finished(self) -> bool:
        return self.finished

    def start_fetch(self) -> 'TimeMeasurement':
        if self.fetch_end > 0:
            return self
        self.fetch_begin = default_timer()
        self.fetching = True
        return self

    def stop_fetch(self) -> 'TimeMeasurement':
        if self.fetch_end > 0:
            return self
        self.fetch_end = default_timer()
        self.fetching = False
        self.timecost = self.timecost + (self.fetch_end - self.fetch_begin)
        return self

    def start_render(self) -> 'TimeMeasurement':
        self.stop_fetch()
        if self.render_end > 0:
            return self
        self.render_begin = default_timer()
        self.rendering = True
        return self

    def stop_render(self) -> 'TimeMeasurement':
        self.stop_fetch()
        if self.render_end > 0:
            return self
        self.render_end = default_timer()
        self.rendering = False
        self.timecost = self.timecost + (self.render_end - self.render_begin)
        self.finished = True
        return self

    def fetch_timecost(self) -> float:
        if self.fetching:
            return default_timer() - self.fetch_begin
        return self.fetch_end - self.fetch_begin

    def render_timecost(self) -> float:
        if self.rendering:
            return default_timer() - self.render_begin
        return self.render_end - self.render_begin

    def accumulated_timecost(self) -> float:
        if self.fetching or self.rendering:
            return default_timer() - self.fetch_begin
        return self.timecost



class ThreadTermination(Exception):
    """
    Indicates that a thread has been terminated from within by some 
    reason.
    """
    pass


class CorruptedImage(Exception):
    """
    Indicate that the response has corrupted image binary, resulting in 
    rendering impossibility.
    """
    pass