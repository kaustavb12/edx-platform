"""
Tests for /save/course/ API.
"""


import ddt
from django.conf import settings
from django.core.cache import cache
from django.urls import reverse
from django.test.utils import override_settings
from unittest.mock import patch
from rest_framework.test import APITestCase
from opaque_keys.edx.keys import CourseKey

from openedx.core.djangolib.testing.utils import skip_unless_lms
from common.djangoapps.third_party_auth.tests.testutil import ThirdPartyAuthTestMixin
from openedx.core.djangoapps.content.course_overviews.tests.factories import CourseOverviewFactory


@skip_unless_lms
@ddt.ddt
class SaveForLaterApiViewTest(ThirdPartyAuthTestMixin, APITestCase):
    """
    Save for later tests
    """

    def setUp(self):  # pylint: disable=arguments-differ
        """
        Test Setup
        """
        super().setUp()

        self.url = reverse('api:v1:save_course')
        self.email = 'test@edx.org'
        self.invalid_email = 'test@edx'
        self.course_id = 'course-v1:TestX+ProEnroll+P'
        self.course_key = CourseKey.from_string(self.course_id)
        CourseOverviewFactory.create(id=self.course_key)

    def test_send_course_using_email(self):
        """
        Test successfully email sent
        """
        with patch('lms.djangoapps.save_for_later.api.v1.views.get_course_organization') as mock_get_org:
            class Logo:
                url = '/logo.png'
            logo = Logo()
            mock_get_org.return_value = {'logo': logo}
            request_payload = {'email': self.email, 'course_id': self.course_id, 'marketing_url': 'http://google.com'}
            response = self.client.post(self.url, data=request_payload)
            assert response.status_code == 200

    @override_settings(
        CACHES={
            'default': {
                'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
                'LOCATION': 'registration_proxy',
            }
        }
    )
    def test_save_for_later_api_rate_limiting(self):
        """
        Test api rate limit
        """
        with patch('lms.djangoapps.save_for_later.api.v1.views.get_course_organization') as mock_get_org:
            class Logo:
                url = '/logo.png'
            logo = Logo()
            mock_get_org.return_value = {'logo': logo}
            request_payload = {
                'email': self.email,
                'course_id': self.course_id,
                'marketing_url': 'http://google.com',
            }
            for _ in range(int(settings.SAVE_FOR_LATER_EMAIL_RATE_LIMIT.split('/')[0])):
                response = self.client.post(self.url, data=request_payload)
                assert response.status_code != 403

            response = self.client.post(self.url, data=request_payload)
            assert response.status_code == 403
            cache.clear()

            for _ in range(int(settings.SAVE_FOR_LATER_IP_RATE_LIMIT.split('/')[0])):
                request_payload['email'] = 'test${_}@edx.org'.format(_=_)
                response = self.client.post(self.url, data=request_payload)
                assert response.status_code != 403

            response = self.client.post(self.url, data=request_payload)
            assert response.status_code == 403
            cache.clear()

    def test_invalid_email_address(self):
        """
        Test email validation
        """
        request_payload = {'email': self.invalid_email, 'course_id': self.course_id}
        response = self.client.post(self.url, data=request_payload)
        assert response.status_code == 400
