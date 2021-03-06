"""
Signal handling functions for use with external commerce service.
"""
from __future__ import unicode_literals

import json
import logging
from urlparse import urljoin

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.dispatch import receiver
from django.utils.translation import ugettext as _

from entitlements.signals import REFUND_ENTITLEMENT
from openedx.core.djangoapps.commerce.utils import ecommerce_api_client, is_commerce_service_configured
from openedx.core.djangoapps.site_configuration import helpers as configuration_helpers
from openedx.core.djangoapps.theming import helpers as theming_helpers
from request_cache.middleware import RequestCache
from student.signals import REFUND_ORDER
from .models import CommerceConfiguration

log = logging.getLogger(__name__)


# pylint: disable=unused-argument
@receiver(REFUND_ORDER)
def handle_refund_order(sender, course_enrollment=None, **kwargs):
    """
    Signal receiver for unenrollments, used to automatically initiate refunds
    when applicable.
    """
    if not is_commerce_service_configured():
        return

    if course_enrollment and course_enrollment.refundable():
        try:
            request_user = get_request_user() or course_enrollment.user
            if isinstance(request_user, AnonymousUser):
                # Assume the request was initiated via server-to-server
                # API call (presumably Otto).  In this case we cannot
                # construct a client to call Otto back anyway, because
                # the client does not work anonymously, and furthermore,
                # there's certainly no need to inform Otto about this request.
                return
            refund_seat(course_enrollment)
        except Exception:  # pylint: disable=broad-except
            # don't assume the signal was fired with `send_robust`.
            # avoid blowing up other signal handlers by gracefully
            # trapping the Exception and logging an error.
            log.exception(
                "Unexpected exception while attempting to initiate refund for user [%s], course [%s]",
                course_enrollment.user.id,
                course_enrollment.course_id,
            )


# pylint: disable=unused-argument
@receiver(REFUND_ENTITLEMENT)
def handle_refund_entitlement(sender, course_entitlement=None, **kwargs):
    if not is_commerce_service_configured():
        return

    if course_entitlement and course_entitlement.is_entitlement_refundable():
        try:
            request_user = get_request_user()
            if request_user and course_entitlement.user == request_user:
                refund_entitlement(course_entitlement)
        except Exception as exc:  # pylint: disable=broad-except
            # don't assume the signal was fired with `send_robust`.
            # avoid blowing up other signal handlers by gracefully
            # trapping the Exception and logging an error.
            log.exception(
                "Unexpected exception while attempting to initiate refund for user [%s], "
                "course entitlement [%s] message: [%s]",
                course_entitlement.user.id,
                course_entitlement.uuid,
                str(exc)
            )


def get_request_user():
    """
    Helper to get the authenticated user from the current HTTP request (if
    applicable).

    If the requester of an unenrollment is not the same person as the student
    being unenrolled, we authenticate to the commerce service as the requester.
    """
    request = RequestCache.get_current_request()
    return getattr(request, 'user', None)


def _process_refund(refund_ids, api_client, course_product, is_entitlement=False):
    """
    Helper method to process a refund for a given course_product
    """
    config = CommerceConfiguration.current()

    if config.enable_automatic_refund_approval:
        refunds_requiring_approval = []

        for refund_id in refund_ids:
            try:
                # NOTE: Approve payment only because the user has already been unenrolled. Additionally, this
                # ensures we don't tie up an additional web worker when the E-Commerce Service tries to unenroll
                # the learner
                api_client.refunds(refund_id).process.put({'action': 'approve_payment_only'})
                log.info('Refund [%d] successfully approved.', refund_id)
            except:  # pylint: disable=bare-except
                log.exception('Failed to automatically approve refund [%d]!', refund_id)
                refunds_requiring_approval.append(refund_id)
    else:
        refunds_requiring_approval = refund_ids

    if refunds_requiring_approval:
        # XCOM-371: this is a temporary measure to suppress refund-related email
        # notifications to students and support for free enrollments.  This
        # condition should be removed when the CourseEnrollment.refundable() logic
        # is updated to be more correct, or when we implement better handling (and
        # notifications) in Otto for handling reversal of $0 transactions.
        if course_product.mode != 'verified':
            # 'verified' is the only enrollment mode that should presently
            # result in opening a refund request.
            msg = 'Skipping refund email notification for non-verified mode for user [%s], course [%s], mode: [%s]'
            course_identifier = course_product.course_id
            if is_entitlement:
                course_identifier = str(course_product.uuid)
                msg = ('Skipping refund email notification for non-verified mode for user [%s], '
                       'course entitlement [%s], mode: [%s]')
            log.info(
                msg,
                course_product.user.id,
                course_identifier,
                course_product.mode,
            )
        else:
            try:
                send_refund_notification(course_product, refunds_requiring_approval)
            except:  # pylint: disable=bare-except
                # don't break, just log a warning
                log.warning('Could not send email notification for refund.', exc_info=True)


def refund_seat(course_enrollment):
    """
    Attempt to initiate a refund for any orders associated with the seat being unenrolled, using the commerce service.

    Arguments:
        course_enrollment (CourseEnrollment): a student enrollment

    Returns:
        A list of the external service's IDs for any refunds that were initiated
            (may be empty).

    Raises:
        exceptions.SlumberBaseException: for any unhandled HTTP error during communication with the E-Commerce Service.
        exceptions.Timeout: if the attempt to reach the commerce service timed out.
    """
    User = get_user_model()  # pylint:disable=invalid-name
    course_key_str = unicode(course_enrollment.course_id)
    enrollee = course_enrollment.user

    service_user = User.objects.get(username=settings.ECOMMERCE_SERVICE_WORKER_USERNAME)
    api_client = ecommerce_api_client(service_user)

    log.info('Attempting to create a refund for user [%s], course [%s]...', enrollee.id, course_key_str)

    refund_ids = api_client.refunds.post({'course_id': course_key_str, 'username': enrollee.username})

    if refund_ids:
        log.info('Refund successfully opened for user [%s], course [%s]: %r', enrollee.id, course_key_str, refund_ids)

        _process_refund(
            refund_ids=refund_ids,
            api_client=api_client,
            course_product=course_enrollment,
        )
    else:
        log.info('No refund opened for user [%s], course [%s]', enrollee.id, course_key_str)

    return refund_ids


def refund_entitlement(course_entitlement):
    """
    Attempt a refund of a course entitlement
    :param course_entitlement:
    :return:
    """
    user_model = get_user_model()
    enrollee = course_entitlement.user
    entitlement_uuid = str(course_entitlement.uuid)

    service_user = user_model.objects.get(username=settings.ECOMMERCE_SERVICE_WORKER_USERNAME)
    api_client = ecommerce_api_client(service_user)

    log.info(
        'Attempting to create a refund for user [%s], course entitlement [%s]...',
        enrollee.username,
        entitlement_uuid
    )

    refund_ids = api_client.refunds.post(
        {
            'order_number': course_entitlement.order_number,
            'username': enrollee.username,
            'entitlement_uuid': entitlement_uuid,
        }
    )

    if refund_ids:
        log.info(
            'Refund successfully opened for user [%s], course entitlement [%s]: %r',
            enrollee.username,
            entitlement_uuid,
            refund_ids,
        )

        _process_refund(
            refund_ids=refund_ids,
            api_client=api_client,
            course_product=course_entitlement,
            is_entitlement=True
        )
    else:
        log.info('No refund opened for user [%s], course entitlement [%s]', enrollee.id, entitlement_uuid)

    return refund_ids


def create_zendesk_ticket(requester_name, requester_email, subject, body, tags=None):
    """ Create a Zendesk ticket via API. """
    if not (settings.ZENDESK_URL and settings.ZENDESK_USER and settings.ZENDESK_API_KEY):
        log.debug('Zendesk is not configured. Cannot create a ticket.')
        return

    # Copy the tags to avoid modifying the original list.
    tags = list(tags or [])
    tags.append('LMS')

    # Remove duplicates
    tags = list(set(tags))

    data = {
        'ticket': {
            'requester': {
                'name': requester_name,
                'email': requester_email
            },
            'subject': subject,
            'comment': {'body': body},
            'tags': tags
        }
    }

    # Encode the data to create a JSON payload
    payload = json.dumps(data)

    # Set the request parameters
    url = urljoin(settings.ZENDESK_URL, '/api/v2/tickets.json')
    user = '{}/token'.format(settings.ZENDESK_USER)
    pwd = settings.ZENDESK_API_KEY
    headers = {'content-type': 'application/json'}

    try:
        response = requests.post(url, data=payload, auth=(user, pwd), headers=headers)

        # Check for HTTP codes other than 201 (Created)
        if response.status_code != 201:
            log.error('Failed to create ticket. Status: [%d], Body: [%s]', response.status_code, response.content)
        else:
            log.debug('Successfully created ticket.')
    except Exception:  # pylint: disable=broad-except
        log.exception('Failed to create ticket.')
        return


def generate_refund_notification_body(student, refund_ids):  # pylint: disable=invalid-name
    """ Returns a refund notification message body. """
    msg = _(
        "A refund request has been initiated for {username} ({email}). "
        "To process this request, please visit the link(s) below."
    ).format(username=student.username, email=student.email)

    ecommerce_url_root = configuration_helpers.get_value(
        'ECOMMERCE_PUBLIC_URL_ROOT', settings.ECOMMERCE_PUBLIC_URL_ROOT,
    )
    refund_urls = [urljoin(ecommerce_url_root, '/dashboard/refunds/{}/'.format(refund_id))
                   for refund_id in refund_ids]

    return '{msg}\n\n{urls}'.format(msg=msg, urls='\n'.join(refund_urls))


def send_refund_notification(course_product, refund_ids):
    """ Notify the support team of the refund request. """

    tags = ['auto_refund']

    if theming_helpers.is_request_in_themed_site():
        # this is not presently supported with the external service.
        raise NotImplementedError("Unable to send refund processing emails to support teams.")

    student = course_product.user
    subject = _("[Refund] User-Requested Refund")
    body = generate_refund_notification_body(student, refund_ids)
    requester_name = student.profile.name or student.username
    create_zendesk_ticket(requester_name, student.email, subject, body, tags)
