"""
Views related to course tabs
"""
from typing import Dict, Iterable, List, Optional

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseNotFound
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
from opaque_keys.edx.keys import CourseKey, UsageKey
from rest_framework.exceptions import ValidationError
from xmodule.course_module import CourseBlock
from xmodule.modulestore import ModuleStoreEnum
from xmodule.modulestore.django import modulestore
from xmodule.tabs import CourseTab, CourseTabList, InvalidTabsException, StaticTab

from common.djangoapps.edxmako.shortcuts import render_to_response
from common.djangoapps.student.auth import has_course_author_access
from common.djangoapps.util.json_request import JsonResponse, JsonResponseBadRequest, expect_json
from ..utils import get_lms_link_for_item

__all__ = ["tabs_handler", "update_tabs_handler"]

User = get_user_model()


@expect_json
@login_required
@ensure_csrf_cookie
@require_http_methods(("GET", "POST", "PUT"))
def tabs_handler(request, course_key_string):
    """
    The restful handler for static tabs.

    GET
        html: return page for editing static tabs
        json: not supported
    PUT or POST
        json: update the tab order. It is expected that the request body contains a JSON-encoded dict with entry "tabs".
        The value for "tabs" is an array of tab locators, indicating the desired order of the tabs.

    Creating a tab, deleting a tab, or changing its contents is not supported through this method.
    Instead use the general xblock URL (see item.xblock_handler).
    """
    course_key = CourseKey.from_string(course_key_string)
    if not has_course_author_access(request.user, course_key):
        raise PermissionDenied()

    course_item = modulestore().get_course(course_key)

    if "application/json" in request.META.get("HTTP_ACCEPT", "application/json"):
        if request.method == "GET":  # lint-amnesty, pylint: disable=no-else-raise
            raise NotImplementedError("coming soon")
        else:
            try:
                update_tabs_handler(course_item, request.json, request.user)
            except ValidationError as err:
                return JsonResponseBadRequest(err.detail)
            return JsonResponse()

    elif request.method == "GET":  # assume html
        # get all tabs from the tabs list and select only static tabs (a.k.a. user-created tabs)
        # present in the same order they are displayed in LMS

        tabs_to_render = list(get_course_static_tabs(course_item, request.user))

        return render_to_response(
            "edit-tabs.html",
            {
                "context_course": course_item,
                "tabs_to_render": tabs_to_render,
                "lms_link": get_lms_link_for_item(course_item.location),
            },
        )
    else:
        return HttpResponseNotFound()


def get_course_static_tabs(course_item: CourseBlock, user: User) -> Iterable[CourseTab]:
    """
    Yields all the static tabs in a course including hidden tabs.

    Args:
        course_item (CourseBlock): The course object from which to get the tabs
        user (User): The user fetching the course tabs.

    Returns:
        Iterable[CourseTab]: An iterable containing course tab objects from the
        course
    """

    for tab in CourseTabList.iterate_displayable(course_item, user=user, inline_collections=False, include_hidden=True):
        if isinstance(tab, StaticTab):
            # static tab needs its locator information to render itself as an xmodule
            static_tab_loc = course_item.id.make_usage_key("static_tab", tab.url_slug)
            tab.locator = static_tab_loc
            yield tab


def update_tabs_handler(course_item: CourseBlock, tabs_data: Dict, user: User) -> None:
    """
    Helper to handle updates to course tabs based on API data.

    Args:
        course_item (CourseBlock): Course module whose tabs need to be updated
        tabs_data (Dict): JSON formatted data for updating or reordering tabs.
        user (User): The user performing the operation.
    """

    if "tabs" in tabs_data:
        reorder_tabs_handler(course_item, tabs_data, user)
    elif "tab_id_locator" in tabs_data:
        edit_tab_handler(course_item, tabs_data, user)
    else:
        raise NotImplementedError("Creating or changing tab content is not supported.")


def reorder_tabs_handler(course_item, tabs_data, user):
    """
    Helper function for handling reorder of static tabs request
    """

    # Tabs are identified by tab_id or locators.
    # The locators are used to identify static tabs since they are xmodules.
    # Although all tabs have tab_ids, newly created static tabs do not know
    # their tab_ids since the xmodule editor uses only locators to identify new objects.
    requested_tab_id_locators = tabs_data["tabs"]

    #get original tab list of only static tabs with their original index(position) in the full course tabs list
    old_tab_dict = {}
    for idx, tab in enumerate(course_item.tabs):
        if isinstance(tab, StaticTab):
            old_tab_dict[tab] = idx
    old_tab_list = list(old_tab_dict.keys())

    new_tab_list = create_new_list(requested_tab_id_locators, old_tab_list)

    # Creates a full new course tab list of both default and static course tabs
    # by looping through the new tab list of static only tabs and
    # putting them in their new position in the list of course item tabs
    # original_idx gives the list of positions of all static tabs in course tabs originally
    full_new_tab_list = course_item.tabs
    original_idx = list(old_tab_dict.values())
    for i in range(len(new_tab_list)):
        full_new_tab_list[original_idx[i]] = new_tab_list[i]

    # validate the tabs to make sure everything is Ok (e.g., did the client try to reorder unmovable tabs?)
    try:
        CourseTabList.validate_tabs(full_new_tab_list)
    except InvalidTabsException as exception:
        raise ValidationError({"error": f"New list of tabs is not valid: {str(exception)}."}) from exception

    # persist the new order of the tabs
    course_item.tabs = full_new_tab_list
    modulestore().update_item(course_item, user.id)


def create_new_list(requested_tab_id_locators, old_tab_list):
    """
    Helper function for creating a new course tab list in the new order
    of reordered tabs
    """
    new_tab_list = []
    for tab_id_locator in requested_tab_id_locators:
        tab = get_tab_by_tab_id_locator(old_tab_list, tab_id_locator)
        if tab is None:
            raise ValidationError({"error": f"Tab with id_locator '{tab_id_locator}' does not exist."})
        new_tab_list.append(tab)

    # the old_tab_list may contain additional tabs that were not rendered in the UI because of
    # global or course settings.  so add those to the end of the list.
    non_displayed_tabs = set(old_tab_list) - set(new_tab_list)
    new_tab_list.extend(non_displayed_tabs)
    return new_tab_list


def edit_tab_handler(course_item: CourseBlock, tabs_data: Dict, user: User):
    """
    Helper function for handling requests to edit settings of a single tab
    """

    # Tabs are identified by tab_id or locator
    tab_id_locator = tabs_data["tab_id_locator"]

    # Find the given tab in the course
    tab = get_tab_by_tab_id_locator(course_item.tabs, tab_id_locator)
    if tab is None:
        raise ValidationError({"error": f"Tab with id_locator '{tab_id_locator}' does not exist."})

    if "is_hidden" in tabs_data:
        if tab.is_hideable:
            # set the is_hidden attribute on the requested tab
            tab.is_hidden = tabs_data["is_hidden"]
            modulestore().update_item(course_item, user.id)
        else:
            raise ValidationError({"error": f"Tab of type {tab.type} can not be hidden"})
    else:
        raise NotImplementedError(f"Unsupported request to edit tab: {tabs_data}")


def get_tab_by_tab_id_locator(tab_list: List[CourseTab], tab_id_locator: Dict[str, str]) -> Optional[CourseTab]:
    """
    Look for a tab with the specified tab_id or locator.  Returns the first matching tab.
    """
    tab = None
    if "tab_id" in tab_id_locator:
        tab = CourseTabList.get_tab_by_id(tab_list, tab_id_locator["tab_id"])
    elif "tab_locator" in tab_id_locator:
        tab = get_tab_by_locator(tab_list, tab_id_locator["tab_locator"])
    return tab


def get_tab_by_locator(tab_list: List[CourseTab], usage_key_string: str) -> Optional[CourseTab]:
    """
    Look for a tab with the specified locator.  Returns the first matching tab.
    """
    tab_location = UsageKey.from_string(usage_key_string)
    item = modulestore().get_item(tab_location)
    static_tab = StaticTab(
        name=item.display_name,
        url_slug=item.location.name,
    )
    return CourseTabList.get_tab_by_id(tab_list, static_tab.tab_id)


# "primitive" tab edit functions driven by the command line.
# These should be replaced/deleted by a more capable GUI someday.
# Note that the command line UI identifies the tabs with 1-based
# indexing, but this implementation code is standard 0-based.

def validate_args(num, tab_type):
    "Throws for the disallowed cases."
    if num <= 1:
        raise ValueError('Tabs 1 and 2 cannot be edited')
    if tab_type == 'static_tab':
        raise ValueError('Tabs of type static_tab cannot be edited here (use Studio)')


def primitive_delete(course, num):
    "Deletes the given tab number (0 based)."
    tabs = course.tabs
    validate_args(num, tabs[num].get('type', ''))
    del tabs[num]
    # Note for future implementations: if you delete a static_tab, then Chris Dodge
    # points out that there's other stuff to delete beyond this element.
    # This code happens to not delete static_tab so it doesn't come up.
    modulestore().update_item(course, ModuleStoreEnum.UserID.primitive_command)


def primitive_insert(course, num, tab_type, name):
    "Inserts a new tab at the given number (0 based)."
    validate_args(num, tab_type)
    new_tab = CourseTab.from_json({'type': str(tab_type), 'name': str(name)})
    tabs = course.tabs
    tabs.insert(num, new_tab)
    modulestore().update_item(course, ModuleStoreEnum.UserID.primitive_command)
