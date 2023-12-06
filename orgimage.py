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
import urllib.parse
import urllib.request
import OrgExtended.orgdb as db
import OrgExtended.asettings as settings
from os import path
from timeit import default_timer
from sublime import Region, View
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
from OrgExtended.orgparse.startup import Startup
from OrgExtended.orgutil.cache import ConstrainedCache
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
    substring_region,
)
from OrgExtended.orgutil.threads import starmap_pools
from OrgExtended.orgutil.typecompat import Literal, Point, TypedDict
from OrgExtended.orgutil.util import at, is_iterable, safe_call, shallow_flatten, split_into_chunks

DEFAULT_POOL_SIZE = 10
ONE_SECOND = 1000
STATUS_ID = 'orgextra_image'

ImageCache = ConstrainedCache.use('image', max_size = 100 * 1024 * 1024) # 100 MB

COMMAND_RENDER_IMAGES = 'org_extra_render_images'
COMMAND_SHOW_IMAGES = 'org_extra_show_images'

SELECTOR_ORG_SOURCE = 'text.orgmode'
SELECTOR_ORG_LINK = 'orgmode.link'
SELECTOR_ORG_LINK_TEXT_HREF = 'orgmode.link.text.href'

SETTING_INSTANT_RENDER = 'extra.image.instantRender'
SETTING_USE_LAZYLOAD = 'extra.image.useLazyload'

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


# Types
Action = Literal['open', 'save', 'unfold', 'unknown']
CachedImage = TypedDict('CachedImage', { 'original_url': str, 'resolved_url': str, 'data_size': Union[int, float] })
OnData = Callable[['CachedImage'], None]
OnError = Callable[[str], None]
OnFinish = Callable[[List['CachedImage']], None]
RegionRange = Literal['folding', 'all', 'pre-section', 'auto']
StartupEnum = Literal[
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
ViewState = TypedDict(
    'ViewState', 
    { 
        'initialized': bool, 
        'last_action': str 
    }
)


def cached_image(url: str, rurl: str, size: int = 0) -> CachedImage:
    """
    Return an object can be used to transfer between commands when one 
    needs to signal to the others that the image has been loaded and 
    cached on memory.
    """
    return { 'original_url': url, 'resolved_url': rurl, 'data_size': size }


def context_data(view: View) -> ViewState:
    """
    Get a context wherein classes with the same view can share data 
    among themselves.
    """
    view_state = ContextData.use(view)
    # We should set the states default here
    if len(view_state) == 0:
        view_state['initialized'] = False
        view_state['last_action'] = ''
    return view_state


def startup(value: StartupEnum) -> StartupEnum:
    """
    Get a value from Startup enum in the manner of a bedridden old man
    """
    return Startup[value]


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
    try:
        attributes = re.findall(REGEX_ORG_ATTR, textline)
        width, height = default_width, default_height
        if textline.strip().startswith('#+ORG_ATTR:') and len(attributes) > 0:
            for attribute in attributes:
                key, value, unit = attribute
                if key == 'width':
                    width = convert_length_to_px(view, value, unit, 'width')
                elif key == 'height':
                    height = convert_length_to_px(view, value, unit, 'height')
        return width, height
    except Exception as error:
        print(error)
        traceback.print_tb(error.__traceback__)
        return default_width, default_height


def fetch_or_load_image(
    url: str, 
    cwd: str, 
    timeout: Optional[float] = None
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
            response = urllib.request.urlopen(absolute_url)
            return response.read()
        # Handle the case of the provided url is already absolute
        elif url.startswith('http:') or url.startswith('https'):
            response = urllib.request.urlopen(url)
            return response.read()
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


def resolve_local(url: str, cwd: str = '/') -> str:
    """
    Convert any case of local path to absolute path, skip remote url.
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
    Convert any case of remote url to absolute url. This function can be
    used to convert relative links in remote .org file to absoluted links.    
    From there we can load images for them.
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


def matching_context(view: View) -> bool:
    """
    Run this function on top of every run() method to filter out most 
    of unappropriate context (early return)
    """
    if not view.match_selector(0, SELECTOR_ORG_SOURCE):
        return False
    return True


class OrgExtraImage(sublime_plugin.EventListener):
    """
    Event handlers
    """
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
        Catches the OrgTabCyclingCommand to add the behavior of 
        automatically loading images when we unfold the content of 
        a section.
        """
        if command != 'org_tab_cycling':
            return None
        lazyload_images = settings.Get(SETTING_USE_LAZYLOAD, False)
        if not lazyload_images:
            return None
        current_status = view.get_status(STATUS_ID)
        if current_status:
            return None
        if self.is_folding_section(view):
            view.run_command(COMMAND_SHOW_IMAGES, { 'region_range': 'folding' })
            self.update_action(view, 'unfold')


    def on_post_save(self, view: View) -> Any:
        """
        May re-render images when the ORG_ATTR values changed
        """
        if not matching_context(view):
            return None
        if not PhantomsManager.is_being_managed(view):
            return None
        file = db.Get().FindInfo(view)
        setting_startup = settings.Get('startup', ['showall'])
        inbuffer_startup = file.org[0].startup(setting_startup)
        prohibit_range = LIST_OUTSIDE_SECTION_REGION_RANGES
        region_range = detect_region_range(view)
        # We should not render things outside the folding section if the 
        # STARTUP: inlineimages is not set, unless you unfolded something
        if region_range in prohibit_range and startup('inlineimages') not in inbuffer_startup:
            return None
        current_status = view.get_status(STATUS_ID)
        # If the last action is save, that's ok to render the image again
        if not current_status or self.last_action(view) == 'save':
            view.run_command(COMMAND_SHOW_IMAGES, 
                args = { 
                    'region_range': 'auto', 
                    'no_download': True, 
                }
            )
            self.update_action(view, 'save')


    def on_pre_close(self, view: sublime.View):
        """
        Should remove relevant data before closing out the view to
        avoid memory leaks.
        """
        PhantomsManager.remove(view)
        ContextData.remove(view)


    @classmethod
    def initialize(cls, view: View, default_action: Action = 'open') -> Any:
        """
        Define some initial properties when a file open
        """
        if not matching_context(view):
            return None

        view_state = context_data(view)
        if view_state.get('initialized') == True:
            return None

        view_state['last_action'] = default_action
        view_state['initialized'] = True


    def autoload(self, view: View) -> None:
        """
        Show all images on the view. This action should only be done once 
        each time the file have opened with the inlineimages startup.
        """
        if not matching_context(view):
            return None
        # Stop if the current view already been rendered
        if PhantomsManager.is_being_managed(view):
            return None
        try:
            file = db.Get().FindInfo(view)
            setting_startup = settings.Get('startup', ['showall'])
            inbuffer_startup = file.org[0].startup(setting_startup)
            PhantomsManager.use(view)
            if startup('inlineimages') in inbuffer_startup:
                view.run_command(COMMAND_SHOW_IMAGES, { 'region_range': 'all' })
        except Exception as error:
            print(error)
            traceback.print_tb(error.__traceback__)


    def is_folding_section(self, view: View) -> bool:
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


    @staticmethod
    def last_action(view: View) -> Action:
        """
        Latest action. 
        """
        view_state = context_data(view)
        return view_state['last_action']


    @staticmethod
    def update_action(view: View, action: Action) -> 'OrgExtraImage':
        """
        Sets the last action.
        """
        view_state = context_data(view)
        view_state['last_action'] = action


class OrgExtraShowImagesCommand(sublime_plugin.TextCommand):
    """
    Non-blocking load and show images in the current .org file. 
    Supports two options of render range:\n
    - 'folding': Loads and renders all images in the folding content. 
      (Default option)\n
    - 'all': Loads and renders all images in the whole document.
      (Auto-applied this option on the file that use #+STARTUP: inlineimages)
    - 'pre-section': Applies to the region preceding the first heading 
      in the document.
    - 'auto': Auto detection.
    """
    def run(
        self, 
        edit, 
        region_range: RegionRange = 'folding',
        no_download: bool = False,
    ):
        view = self.view
        try:
            if not matching_context(view):
                return None
            
            if region_range == 'auto':
                region_range = detect_region_range(view)

            selected_region = self.select_region(region_range)
            image_regions = self.collect_image_regions(selected_region)
            dimension_changed, phantomless = self.ignore_rendered_regions(image_regions)
            image_regions = dimension_changed + phantomless

            if not image_regions:
                return self.handle_nothing_to_show(status_duration = 3)
            if len(phantomless):
                self.create_placeholder_phantoms(phantomless)

            urls = self.collect_image_urls(image_regions)

            if no_download:
                return self.handle_rendering_without_download(
                    region = selected_region,
                    urls = urls,
                    cwd = self.get_url_from_scratch(self.view) \
                        if self.view.is_scratch() \
                        else path.dirname(self.view.file_name() or ''),
                )

            status = self.use_status_indicator(region_range, len(urls))
            pools = split_into_chunks(list(urls), DEFAULT_POOL_SIZE)
            
            status.start()
            
            # Load images in a scratch buffer view (remote .org file) in
            # an other way
            if self.view.is_scratch():
                sublime.set_timeout_async(
                    lambda: self.parallel_requests_using_threads(
                        pools = pools,
                        cwd = self.get_url_from_scratch(self.view),
                        on_data = self.on_downloaded(selected_region, lambda: status.succeed()),
                        on_error = lambda: status.failed(),
                        on_finish = self.on_threads_finished(selected_region, status)))
            else:
                sublime.set_timeout_async(
                    lambda: self.parallel_requests_using_threads(
                        pools = pools,
                        cwd = path.dirname(self.view.file_name() or ''),
                        on_data = self.on_downloaded(selected_region, lambda: status.succeed()),
                        on_error = lambda: status.failed(),
                        on_finish = self.on_threads_finished(selected_region, status)))

            sublime.set_timeout(
                lambda: status.is_running() and status.set('Slow internet! Be patient...'),
                len(urls) * 5 * 1000) # Limiting 5s for each downloading image
        except Exception as error:
            show_message(error, level = 'error')
            traceback.print_tb(error.__traceback__)


    def collect_image_regions(self, region: Region) -> List[Region]:
        """
        Collect all inlineimage regions in the specified region.
        """
        href_regions = find_by_selector_in_region(self.view, region, SELECTOR_ORG_LINK_TEXT_HREF)
        image_regions = []
        for region in href_regions:
            url = self.view.substr(region)
            is_image = any(url.endswith(ext) for ext in LIST_SUPPORTED_IMAGE_EXTENSIONS)
            if is_image:
                image_regions.append(region)
        return image_regions


    def create_placeholder_phantoms(self, regions: Region) -> None:
        """
        Should pre-create phantoms to tracking the regions for in-time 
        rendering instead of after all. It enhances the user experience 
        and allows for file editing while waiting for images to load.
        """
        pm = PhantomsManager.use(self.view)
        for image_region in regions:
            if pm.has_phantom(image_region):
                pid = pm.get_pid_by_region(image_region)
                pm.erase_phantom(pid)
            pm.add_phantom(image_region, '')


    def collect_image_urls(self, regions: List[Region]) -> Set[str]:
        """
        Collect urls as string from their regions (with duplicate 
        removed to optimize the downloading time).
        """
        urls = map(lambda r: self.view.substr(r), regions)
        return set(urls)


    def get_url_from_scratch(self, view: View) -> str:
        """
        A dumb way to get the url from a buffer that is opening a 
        remote .org file
        """
        name = view.name()
        if name.strip().startswith('[org-remote]'):
            url = name.replace('[org-remote]', '').strip()
            return url
        return ''


    def handle_nothing_to_show(self, status_duration: int) -> None:
        """
        Should only call this method with a return statement when the 
        view has nothing to update
        
        :param      status_duration:  Delay in second to clear the status message
        """
        return set_temporary_status(self.view, STATUS_ID, 'Nothing to render.', status_duration)


    def handle_rendering_without_download(
        self, 
        region: Region,
        urls: List[str], 
        cwd: str
    ) -> None:
        """
        When this method is called, nothing new will be cached, so the 
        render command may reuse what has already been cached.
        """
        tuple_region = region.to_tuple()
        images = map(lambda url: cached_image(url, resolve_local(url, cwd)), urls)
        instant_render_enabled = settings.Get(SETTING_INSTANT_RENDER, False)
        if not instant_render_enabled:
            return self.view.run_command(COMMAND_RENDER_IMAGES,
                args = {
                    'region': tuple_region,
                    'images': images
                }
            )
        else:
            for image in images:
                self.view.run_command(COMMAND_RENDER_IMAGES,
                    args = {
                        'region': tuple_region,
                        'images': [image]
                    }
                )


    def ignore_rendered_regions(self, regions: List[Region]) -> Tuple[List[Region], List[Region]]:
        """
        Filter out the regions that have been rendered by checking their
        existence on PhantomsManager.
        These are some exceptions that would be ignored by the filter 
        to re-rendering:
        - When ORG_ATTR has added or removed
        - When the :width or :height values of ORG_ATTR have changed
        """
        pm = PhantomsManager.use(self.view)
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
            attr_state = self.with_dimension_attributes(upper_line_region)

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
        then: Optional[OnData] = None) -> OnData:
        """
        Implement the instant render option.
        """
        def on_data(cached_image: CachedImage):
            instant_render_enabled = settings.Get(SETTING_INSTANT_RENDER, False)
            if instant_render_enabled:
                self.view.run_command(COMMAND_RENDER_IMAGES, 
                    args = {
                        'region': region.to_tuple(),
                        'images': [cached_image],
                    }
                )
            if callable(then): safe_call(then, [cached_image])
        return on_data


    def on_threads_finished(
        self,
        region: Region,
        status: SublimeStatusIndicator
    ) -> Callable[[List[CachedImage], int], None]:
        """
        Chaining calls the next command to auto-render the cached images
        """
        def on_finish(cached_images: List[CachedImage], timecost: int):
            status.stop(timecost)
            instant_render_enabled = settings.Get(SETTING_INSTANT_RENDER, False)
            if not instant_render_enabled:
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
        on_data: Optional[OnData] = None,
        on_error: Optional[OnError] = None,
        on_finish: Optional[OnFinish] = None,
        timeout: Optional[float] = None
    ) -> None:
        """
        Asynchronously run fetching image operations, which each one
        take a list of urls/filepaths that would be loaded one after 
        another.
        These are the measurement results can be compare to 
        `.parallel_requests_using_threads()`: \n
        - 1 image link: 1.482095375999961, 1.335890114999529, 1.298129551000784
        - 10 image links: 8.051044771000306, 7.954113742000118, 8.047068934999515
        - 85 image links: 93.40657268199993
        I don't quite understand the mechanism behind this API, nor 
        whether I am using it correctly but it doesn't seem 
        significantly faster compared to the synchronous.
        """
        start = default_timer()
        cached_images = []
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
                timeout),
            args = pools,
            on_finish = async_all_finish)


    def parallel_requests_using_threads(
        self,
        pools: List[List[str]],
        cwd: str,
        on_data: Optional[OnData] = None,
        on_error: Optional[OnError] = None,
        on_finish: Optional[OnFinish] = None,
        timeout: Optional[float] = None
    ) -> List[CachedImage]:
        """
        Run threads of fetching image operation in parallel, which each 
        thread take a list of urls/filepaths that would be loaded one 
        after another.
        These are the measurement results can be compare to 
        `.parallel_requests_using_sublime_timeout()`: \n
        * 1 image link: 1.1709886939997887, 1.2005657659992721, 1.2650837740002316 \n
        * 10 image links: 1.387642825000512, 1.5033762000002753, 2.1559584730002825 \n
        * 85 image links: 16.07192872700034, 12.695106612000018, 9.564963673999955 \n
        As you see, it's up to ten times faster. \n
        And of course it's way more faster compared to the traditional method.
        """
        start = default_timer()
        results = shallow_flatten(
            starmap_pools(
                lambda urls: self.thread_execution(
                    urls, 
                    cwd, 
                    on_data, 
                    on_error, 
                    timeout),
                pools
            ))
        end = default_timer()
        if callable(on_finish):
            safe_call(on_finish, [results, end - start])
        return results


    def select_region(self, render_range: RegionRange) -> Region:
        """
        Select the appropriate region based on the given range 
        (folding or all)
        """
        if render_range == 'pre-section':
            prev_, next_ = find_headings_around_cursor(self.view, Region(0, 0))
            return self.select_region('all') if not next_ else region_between(self.view, Region(0, 0), next_)
        if render_range == 'folding':
            cursor_region = get_cursor_region(self.view)
            prev_, next_ = find_headings_around_cursor(self.view, cursor_region)
            return region_between(self.view, prev_, next_)
        return Region(0, self.view.size())


    def thread_execution(
        self,
        urls: List[str],
        cwd: str,
        on_data: Optional[OnData] = None,
        on_error: Optional[OnError] = None,
        timeout: Optional[float] = None
    ) -> List[CachedImage]:
        """
        This method specifies what a thread will have to perform.
        It's typically revolves around ensuring that the images have 
        been cached and are ready for rendering.
        """
        loaded_images = []
        for url in urls:
            resolved_url = resolve_local(url, cwd)
            loaded_binary = None
            cached_binary = ImageCache.get(resolved_url)
            if type(cached_binary) is bytes:
                loaded_binary = cached_binary
                loaded_image = cached_image(url, resolved_url, len(loaded_binary))
                loaded_images.append(loaded_image)
            else:
                loaded_binary = fetch_or_load_image(url, cwd, timeout)
                loaded_image = cached_image(url, resolved_url, len(loaded_binary or ''))
                loaded_images.append(loaded_image)
                if type(loaded_binary) is bytes:
                    ImageCache.set(resolved_url, loaded_binary)
            if loaded_binary is None and callable(on_error):
                safe_call(on_error, [url])
            elif callable(on_data):
                safe_call(on_data, [loaded_image])
        return loaded_images


    def use_status_indicator(
        self, 
        region_range: RegionRange,
        total_count: Optional[int] = None,
        update_interval: Optional[int] = 100,
    ) -> SublimeStatusIndicator:
        """
        Setup a loading indicator on status bar
        """
        mode = 'inlineimages' if region_range == 'all' else 'unfold'
        area = 'current document' if region_range == 'all' else 'folding content'
        return SublimeStatusIndicator(self.view, STATUS_ID,
            message = '[{}] Fetching image in the {}...'.format(mode, area),
            finish_message = 'Done! Rendering the view...',
            total_count = total_count,
            update_interval = update_interval)


    def with_dimension_attributes(self, region: Region) -> bool:
        """
        Return True if a region line contains an ORG_ATTR comment
        with value. In other words, if the line is just `#+ORG_ATTR:`,
        it still returns False.
        """
        substr = self.view.substr(region)
        width, height = extract_dimensions_from_attrs(self.view, substr)
        return width > 0 or height > 0


class OrgExtraRenderImagesCommand(sublime_plugin.TextCommand):
    """
    Render cached image to the view with the ideal size.
    """
    def run(
        self, 
        edit, 
        region: Optional[Tuple[int, int]] = None,
        images: Optional[List[CachedImage]] = [],
    ):
        try:
            if not matching_context(self.view) or not region:
                return None
            region = Region(*region)
            lines = lines_from_region(self.view, region)
            viewport_width, _ = self.view.viewport_extent()
            link_regions = find_by_selector_in_region(self.view, region, SELECTOR_ORG_LINK)
            image_dict = self.to_resolved_url_dict(images)
            original_urls = image_dict.keys()
            for lr in link_regions:
                substr = self.view.substr(lr)
                line_region = self.view.line(lr)
                line_text = self.view.substr(line_region)
                index = lines.index(line_text)
                prev_line = lines[index - 1] if index > 0 else None
                for url, _description in re.findall(REGEX_ORG_LINK, substr):
                    resolved_url = image_dict.get(url)
                    image_binary = ImageCache.get(resolved_url)
                    if not url in original_urls or not image_binary:
                        continue
                    url_region = substring_region(self.view, lr, url)                    
                    width, height, is_resized = self.extract_image_dimensions(prev_line, image_binary)
                    width, height = self.fit_to_viewport(viewport_width, width, height)
                    self.render_image(
                        url_region,
                        image_binary,
                        width,
                        height,
                        len(line_text) - len(line_text.lstrip()),
                        is_resized)
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
            pm = PhantomsManager.use(self.view)
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
        Converting a list of images into a dict that we can use the current 
        relative link to quickly get the corresponding resolved absolute 
        link.
        """
        resolved_url_dict = dict()
        for image in images:
            original_url = image.get('original_url')
            if original_url is None:
                continue
            resolved_url_dict[original_url] = image.get('resolved_url')
        return resolved_url_dict