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
from timeit import default_timer
from sublime import Region, View
from collections import defaultdict
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
    lines_from_region, 
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
STATUS_ID = 'orgextra_image'
THREAD_NAME = 'orgextra_images'
ON_CHANGE_KEY = 'orgextra_image'
SETTING_FILENAME = settings.configFilename + ".sublime-settings"

COMMAND_RENDER_IMAGES = 'org_extra_render_images'
COMMAND_SHOW_IMAGES = 'org_extra_show_images'
COMMAND_HIDE_THIS_IMAGE = 'org_extra_hide_this_image'
COMMAND_HIDE_IMAGES = 'org_extra_hide_images'

EVENT_VIEWPORT_RESIZE = EventSymbol('viewport resize')

SELECTOR_ORG_SOURCE = 'text.orgmode'
SELECTOR_ORG_LINK = 'orgmode.link'
SELECTOR_ORG_LINK_TEXT_HREF = 'orgmode.link.text.href'

SETTING_INSTANT_RENDER = 'extra.image.instantRender'
SETTING_USE_LAZYLOAD = 'extra.image.useLazyload'
SETTING_VIEWPORT_SCALE = 'extra.image.viewportScale'
SETTING_CACHE_SIZE = 'extra.image.cacheSize'

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
    'region': Optional[str],
})
PhantomRefData = TypedDict('PhantomRefData', {
    'width': float,
    'height': float,
    'with_attr': bool,
    'is_hidden': bool,
})
OnDataHandler = Callable[['CachedImage', List['CachedImage']], None]
OnErrorHandler = Callable[[str], None]
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


def pre_close_event(view: View) -> str:
    """
    Returns a string that can be used as a key to emit the close event for
    a specific view.
    """
    return 'close_' + str(view.id())


def cached_image(url: str, rurl: str, size: int = 0) -> CachedImage:
    """
    Return an object can be used to transfer between commands when one 
    needs to signal to the others that the image has been loaded and 
    cached on memory.
    """
    return { 'original_url': url, 'resolved_url': rurl, 'data_size': size }


def context_data(view: View) -> ViewStates:
    """
    Get a context wherein classes with the same view can share data 
    among themselves.
    """
    view_state = ContextData.use(view)
    # We should set the states default here
    if 'initialized' not in view_state:
        view_state['initialized'] = False
    if 'prev_action' not in view_state:
        view_state['prev_action'] = ''
    if 'viewport_extent' not in view_state:
        view_state['viewport_extent'] = view.viewport_extent()
    return view_state


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
) -> Optional[bytes]:
    """
    Remote fetch or load from local the image binary based on the 
    matching case of provided url.

    :param      url:      Accepts url or local file path
    :param      timeout:  Timeout for HTTP requests (None by default).
    """
    try:
        # Handle the case of the current working directory is a http/https url
        if cwd.startswith('http:') or cwd.startswith('https:'):
            relative_path = url
            absolute_url = resolve_remote(relative_path, cwd)
            response = download_binary(
                absolute_url,
                timeout,
                chunk_size,
                termination_hook)
            return response
        # Handle the case of the provided url is already absolute
        elif url.startswith('http:') or url.startswith('https'):
            response = download_binary(
                url,
                timeout,
                chunk_size,
                termination_hook)
            return response
        # Handle the case of the provided url is a local file path
        else:
            relative_path = url
            absolute_path = resolve_local(relative_path, cwd)
            with open(absolute_path, 'rb') as file:
                return file.read()
    except Exception as error:
        print(error)
        traceback.print_tb(error.__traceback__)
        return None


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
        region_range = detect_region_range(view)
        prohibit_range = LIST_OUTSIDE_SECTION_REGION_RANGES
        inbuffer_startup = self.get_inbuffer_startup(view)
        # We should not render things outside the folding section if the 
        # STARTUP: inlineimages is not set, unless you unfolded something
        if region_range in prohibit_range and startup('inlineimages') not in inbuffer_startup:
            return None
        current_status = view.get_status(STATUS_ID)
        # If the last action is `save`, that's ok to render the image again
        if not current_status or self.get_previous_action(view) == 'save':
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
            pre_close = pre_close_event(view)
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
            image_regions = collect_image_regions(view, selected_region)
            dimension_changed, phantomless = self.ignore_unchanged(
                image_regions,
                show_hidden)
            image_regions = dimension_changed + phantomless

            # Load images in a scratch buffer view (remote .org file) in
            # a different manner.
            cwd = get_cwd(view)

            # Show a status message: Nothing to render.
            if not image_regions:
                return self.handle_nothing_changed(status_duration = 3)
            if len(phantomless):
                self.create_placeholder_phantoms(phantomless)

            urls = self.collect_image_urls(image_regions)
            cached_urls, noncached_urls = self.prioritize_cached(urls, cwd)

            if no_download:
                return self.handle_direct_rendering(
                    region = selected_region,
                    urls = urls,
                    cwd = cwd,
                )

            status = self.use_status_indicator(region_range, len(urls))
            status.start()

            if cached_urls:
                # The cached images should ignore the extra.image.instantRender 
                # setting; otherwise, the main thread will be blocked during file 
                # rescanning due to multiple render command callbacks. This is the 
                # cause of the slowness when rendering so much images in a 
                # large-sized file.
                sublime.set_timeout_async(
                    lambda: self.parallel_requests_using_threads(
                        pools = split_into_chunks(list(cached_urls), DEFAULT_POOL_SIZE),
                        cwd = cwd,
                        on_data = lambda: status.succeed(),
                        on_error = lambda: status.failed(),
                        on_finish = self.on_threads_finished(
                            selected_region,
                            lambda timecost: status.stop(timecost) if not noncached_urls else None
                        )
                    )
                )

            # Download non-cached images
            sublime.set_timeout_async(
                lambda: self.parallel_requests_using_threads(
                    pools = split_into_chunks(list(noncached_urls), DEFAULT_POOL_SIZE),
                    cwd = cwd,
                    on_data = self.on_downloaded(selected_region, lambda: status.succeed()),
                    on_error = lambda: status.failed(),
                    on_finish = self.on_threads_finished(selected_region, lambda timecost: status.stop(timecost))
                )
            )
                        
            # Limiting to 5 seconds for each image download.
            # If all images exceed this time threshold, the status 
            # message below will be set.
            sublime.set_timeout(
                lambda: status.is_running() \
                    and status.set('Slow internet! Be patient...'),
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
        for image_region in regions:
            if pm.has_phantom(image_region):
                pid = pm.get_pid_by_region(image_region)
                pm.erase_phantom(pid)
            pm.add_phantom(image_region, '')


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

    def ignore_unchanged(
        self, 
        regions: List[Region],
        show_hidden: bool = True) -> Tuple[List[Region], List[Region]]:
        """
        Filter out the regions that have already been rendered by 
        checking their existence in PhantomsManager. There are some 
        exceptions that will be included by the filter for re-rendering:
        - When ORG_ATTR has been added or removed.
        - When the :width or :height values of ORG_ATTR have changed.
        """
        pm = PhantomsManager.of(self.view)
        lines = self.view.lines(Region(0, self.view.size()))
        rendered_regions = pm.get_all_overseeing_regions()
        phantomless_regions = []
        dimension_changed_regions = []
        for region in regions:
            if region not in rendered_regions:
                phantomless_regions.append(region)
                continue

            current_line = self.view.line(region)
            index = lines.index(current_line)
            if index < 0:
                return dimension_changed_regions, phantomless_regions
            last_pid = pm.get_pid_by_region(region)
            last_data = pm.get_data_by_pid(last_pid) or {}
            upper_line_region = lines[index - 1]
            attr_state = with_dimension_attributes(self.view, upper_line_region)

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
                    self.view,
                    self.view.substr(upper_line_region),
                    default_width = last_data.get('width', -1),
                    default_height = last_data.get('height', -1)
                )
                if width != last_data.get('width') or height != last_data.get('height'):
                    dimension_changed_regions.append(region)

        return dimension_changed_regions, phantomless_regions


    def on_downloaded(
        self,
        region: Region,
        then: Optional[OnDataHandler] = None) -> OnDataHandler:
        """
        Implement the instant render behaviour.
        """
        def on_data(cached_image: CachedImage, images: List[CachedImage]):
            instant_render = settings.Get(SETTING_INSTANT_RENDER, False)
            # Render that image and exclude it from re-rendering in the 
            # 'finish' event.
            if instant_render:
                self.view.run_command(COMMAND_RENDER_IMAGES, 
                    args = {
                        'region': region.to_tuple(),
                        'images': [cached_image],
                    }
                )
                images.remove(cached_image)
            if callable(then): safe_call(then, [cached_image])
        return on_data


    def on_threads_finished(
        self,
        region: Region,
        then: Optional[Callable[[int], None]] = None
    ) -> Callable[[List[CachedImage], int], None]:
        """
        Chaining calls the render command to automatically render the 
        cached images.
        """
        def on_finish(cached_images: List[CachedImage], timecost: int):
            if callable(then):
                safe_call(then, [timecost])
            self.view.run_command(COMMAND_RENDER_IMAGES, 
                args = {
                    'region': region.to_tuple(),
                    'images': cached_images,
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
        stop = pre_close_event(self.view)
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
        timeout: Optional[float] = None
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
        pre_close = pre_close_event(self.view)
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
                        flag),
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
                    termination_hook = termination_hook)
                loaded_image = cached_image(url, resolved_url, len(loaded_binary or ''))
                loaded_images.append(loaded_image)
                if type(loaded_binary) is bytes:
                    ImageCache.set(resolved_url, loaded_binary)
            if isinstance(stop_event, threading.Event):
                if stop_event.is_set():
                    raise ThreadTermination()
            if loaded_binary is None and callable(on_error):
                safe_call(on_error, [url])
            elif callable(on_data):
                safe_call(on_data, [loaded_image, loaded_images])
        return loaded_images


    def use_status_indicator(
        self, 
        region_range: RegionRange,
        total_count: Optional[int] = None,
        update_interval: Optional[int] = 100,
    ) -> SublimeStatusIndicator:
        """
        Setup a loading indicator with messages on the status bar
        """
        mode = 'inlineimages' if region_range == 'all' else 'unfold'
        area = 'current document' if region_range == 'all' else 'folding content'
        return SublimeStatusIndicator(self.view, STATUS_ID,
            message = '[{}] Fetching image in the {}...'.format(mode, area),
            finish_message = 'Done! Rendering the view...',
            total_count = total_count,
            update_interval = update_interval)



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
                        pm.add_phantom(region, '', { 'is_hidden': True })
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
                        pm.add_phantom(region, '', { 'is_hidden': True })

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
        async_rendering: bool = False, 
    ) -> None:
        try:
            if not matching_context(self.view) or not region:
                return None
            render = lambda: None
            if rescan:
                render = lambda: self.handle_rescan_render(region, images)
            else:
                render = lambda: self.handle_unsafe_render(images)
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


    def get_image_binary(self, image: CachedImage, cwd: str) -> Optional[bytes]:
        """
        Retrieve image binary from one of the keys provided by the 
        CachedImage object.
        """
        resolved_url = None
        if image.get('resolved_url'):
            resolved_url = image['resolved_url']
        elif image.get('original_url'):
            url = image.get('original_url')
            resolved_url = resolve_local(url, cwd)
        elif image.get('region'):
            tuple_region = image.get('region')
            if type(tuple_region) is tuple:
                region = Region(*tuple_region)
                url = self.view.substr(region)
                resolved_url = resolve_local(url, cwd)
        return ImageCache.get(resolved_url)


    def handle_rescan_render(
        self,
        tuple_region: Optional[Tuple[int, int]],
        images: List[CachedImage] = [],
    ) -> None:
        """
        Rescan the region before rendering.
        """
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


    def handle_unsafe_render(
        self,
        images: List[CachedImage] = []
    ) -> None:
        """
        Render without rescanning but requires regions where the image 
        urls are located.
        """
        entire = Region(0, self.view.size())
        lines = lines_from_region(self.view, entire)
        cwd = get_cwd(self.view)
        viewport_width, _ = self.view.viewport_extent()
        for image in images:
            tuple_region = image.get('region')
            image_binary = self.get_image_binary(image, cwd)
            if not image_binary or not tuple_region:
                continue
            region = Region(*tuple_region)
            line_region = self.view.line(region)
            line_text = self.view.substr(line_region)
            index = lines.index(line_text)
            preceding_line = lines[index - 1] if index > 0 else None
            indent_level = len(line_text) - len(line_text.lstrip())
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


    def render_image(
        self,
        image_region: Region,
        image_binary: bytes,
        image_width: Width,
        image_height: Height,
        indent_level: int = 0, 
        with_attr: bool = False) -> bool:
        """
        Render the image
        """
        try:
            width, height = int(image_width), int(image_height)
            image_format = image_format_of(image_binary)
            space_indent = '&nbsp;' * (indent_level * 2)
            pm = PhantomsManager.of(self.view)
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
            
            pm.add_phantom(image_region, html, { 
                'width': width, 
                'height': height,
                'with_attr': with_attr })
            return True
        except Exception as error:
            print(error)
            traceback.print_tb(error.__traceback__)
            return False


    def to_resolved_url_dict(self, images: List[CachedImage]) -> Dict[str, str]:
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


class ThreadTermination(Exception):
    """
    Indicates that a thread has been terminated from within by some 
    reason.
    """
    pass
