import re
import traceback
import sublime
import sublime_plugin
from sublime import Region, View
from typing import Callable, Dict, List, Set, Tuple, Union
from OrgExtended.orgutil.sublime_utils import (
    PhantomsManager,
    SublimeStatusIndicator,
    find_by_selectors,
    find_by_selector_in_region,
    get_cursor_region, 
    region_between, 
    show_message,
    slice_regions,
)
from OrgExtended.orgutil.util import split_into_chunks
from OrgExtended.orgutil.typecompat import Literal, Point

STATUS_ID = 'orgextra_image'
DEFAULT_POOL_SIZE = 10
ONE_SECOND = 1000

SELECTOR_ORG_SOURCE = 'text.orgmode'
SELECTOR_ORG_LINK_TEXT_HREF = 'orgmode.link.text.href'

REGEX_ORG_ATTR = re.compile(r":(\w+)\s(\d+)")

LIST_SUPPORTED_IMAGE_EXTENSIONS = [".jpg", ".jpeg", ".png", ".gif"]
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
RegionRange = Literal['folding', 'all']


def extract_dimensions_from_attrs(
    textline: str,
    default_width: int = -1,
    default_height: int = -1) -> Tuple[int, int]:
    """
    Extract width and height from the #+ORG_ATTR line above a link.
    """
    attributes = re.findall(REGEX_ORG_ATTR, textline)
    width, height = default_width, default_height
    if textline.strip().startswith('#+ORG_ATTR:') and len(attributes) > 0:
        for attribute in attributes:
            key, value = attribute
            if key == 'width':
                width = int(value)
            elif key == 'height':
                height = int(value)
    return width, height


def get_current_headline(point: Point, hregions: List[Region]) -> Region:
    """
    Among many headings, find the heading that has the caret on it
    """
    for region in hregions:
        if region.contains(point):
            return region


def get_headings_around_cursor(
    view: View, 
    sel: Region) -> Tuple[Region, Region]:
    """
    Create a region that includes two headlines are surrounding the cursor 
    at any levels.

    :returns:   A tuple containing the region of the previous and next
                headlines.
    """
    begin, end = sel
    regions = find_by_selectors(view, LIST_HEADLINE_SELECTORS)
    cursor_on_heading = any(
        view.match_selector(begin, selector) for selector in LIST_HEADLINE_SELECTORS)
    if cursor_on_heading:
        prev_ = get_current_headline(begin, regions)
        next_ = slice_regions(regions, begin = prev_.end() + 1)[0]
    else:
        prev_ = slice_regions(regions, end = end)[-1]
        next_ = slice_regions(regions, begin = begin + 1)[0]
    return prev_, next_


def matching_context(view: View) -> bool:
    """
    Run this function on top of every run() method to filter out most 
    of unappropriate context (early return)
    """
    if view.is_scratch():
        return False
    if view.match_selector(0, SELECTOR_ORG_SOURCE):
        return False
    return True


class OrgExtraShowImagesCommand(sublime_plugin.TextCommand):
    """
    Show images in the current .org file.
    Supports two options of render range:\n
    - 'folding': Loads and renders all images in the folding content. 
      (Default option)\n
    - 'all': Loads and renders all images in the whole document.
      (Auto-applied this option on the file that use #+STARTUP: inlineimages)
    """
    def run(self, edit, region_range = 'folding'):
        view = self.view
        try:
            if not matching_context(view):
                return None
            
            selected_region = self.select_region(region_range)
            image_regions = self.collect_image_regions(selected_region)
            image_regions = self.ignore_rendered_regions(image_regions)

            if not image_regions:
                return self.handle_nothing_to_show(status_duration = 3)
            urls = self.collect_image_urls(image_regions)
            status = self.use_status_indicator(region_range, len(urls))
            url_pools = split_into_chunks(urls, DEFAULT_POOL_SIZE)
            
            view.set_read_only(True)
            view.set_status('read_only_mode', 'readonly')
            status.start()
            
            # Load images in a scratch buffer view (remote .org file) in
            # an other way
            if self.view.is_scratch():
                pass
            else:
                pass

            # Incomplete
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


    def collect_image_urls(self, regions: List[Region]) -> Set[str]:
        """
        Collect urls as string from their regions (with duplicate 
        removed to optimize the downloading time).
        """
        urls = map(lambda r: self.view.substr(r), regions)
        return set(urls)


    def handle_nothing_to_show(self, status_duration: int) -> None:
        """
        Should only call this method with a return statement when the 
        view has nothing to update
        
        :param      delay:  Delay in second to clear the status message
        """
        self.view.erase_status(STATUS_ID)
        self.view.set_status(STATUS_ID, 'Nothing to render.')
        return sublime.set_timeout(lambda: self.view.erase_status(STATUS_ID), status_duration)


    def ignore_rendered_regions(self, regions: List[Region]) -> List[Region]:
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
            if index <= 0:
                return False
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
                    self.view.substr(upper_line_region),
                    default_width = last_data.get('width', -1),
                    default_height = last_data.get('height', -1)
                )
                if width != last_data.get('width') or height != last_data.get('height'):
                    dimension_changed_regions.append(region)

        # The ones has dimension changed should be prioritized to render
        # first.
        return dimension_changed_regions + phantomless_regions


    def on_thread_finished(
        self,
        region: Region,
        status: SublimeStatusIndicator
    ) -> Callable[[List[Dict], int], None]:
        """
        Chaining calls the next command to auto-render the cached images
        """
        def on_finish(loaded_urls: List[Dict], timecost: int):
            status.stop(timecost)
            self.view.set_read_only(False)
            self.view.set_status('read_only_mode', 'writeable')
            self.view.run_command('org_extra_render_images', 
                args = {
                    'region': region.to_tuple(),
                    'urllist': loaded_urls,
                }
            )
        return on_finish


    def parallel_requests_using_sublime_timeout(
        self,
        pools: List[List[str]],
        cwd: str,
        on_data: Union[Callable[[Dict], None], None] = None,
        on_error: Union[Callable[[str], None], None] = None,
        on_finish: Union[Callable[[List[Dict], float], None], None] = None,
        timeout: Union[float, None] = None,
    ) -> None:
        pass


    def parallel_requests_using_threads(
        self,
        pools: List[List[str]],
        cwd: str,
        on_data: Union[Callable[[Dict], None], None] = None,
        on_error: Union[Callable[[str], None], None] = None,
        on_finish: Union[Callable[[List[Dict], float], None], None] = None,
        timeout: Union[float, None] = None,
    ) -> None:
        pass


    def select_region(self, render_range: RegionRange) -> Region:
        """
        Select the appropriate region based on the given range 
        (folding or all)
        """
        if render_range == 'folding':
            cursor_region = get_cursor_region(self.view)
            prev, next = get_headings_around_cursor(self.view, cursor_region)
            return region_between(self.view, prev, next)
        return Region(0, self.view.size())


    def use_status_indicator(
        self, 
        region_range: RegionRange,
        total_count: Union[int, None] = None,
        update_interval: int = 100,
    ) -> SublimeStatusIndicator:
        """
        Setup a loading indicator on status bar
        """
        mode = 'inlineimages' if region_range == 'all' else 'unfold'
        area = 'whole document' if region_range == 'all' else 'folding content'
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
        width, height = extract_dimensions_from_attrs(substr)
        return width > 0 and height > 0
