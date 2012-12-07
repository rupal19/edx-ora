from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from datetime import datetime
import logging
import os
import json
from statsd import statsd

from models import Submission, Grader
from models import GraderStatus, SubmissionState
import util
import grader_util
from staff_grading import staff_grading_util
from basic_check import basic_check_util
from metrics import timing_functions

log = logging.getLogger(__name__)

_INTERFACE_VERSION = 1

@csrf_exempt
@login_required
@statsd.timed('open_ended_assessment.grading_controller.controller.xqueue_interface.time', tags=['function:submit'])
def submit(request):
    '''
    Xqueue pull script posts objects here.
    Input:

    request - dict with keys xqueue_header and xqueue_body
    xqueue_header needs submission_id,submission_key,queue_name
    xqueue_body needs grader_payload, student_info, student_response, max_score
    grader_payload needs location, course_id,problem_id,grader
    student_info needs anonymous_student_id, submission_time

    Output:
    Returns status code indicating success (0) or failure (1) and message
    '''
    if request.method != 'POST':
        return util._error_response("'submit' must use HTTP POST", _INTERFACE_VERSION)
    else:
        #Minimal parsing of reply
        reply_is_valid, header, body = _is_valid_reply(request.POST.copy())

        if not reply_is_valid:
            log.error("Invalid xqueue object added: request_ip: {0} request.POST: {1}".format(
                util.get_request_ip(request),
                request.POST,
            ))
            statsd.increment("open_ended_assessment.grading_controller.controller.xqueue_interface.submit",
                tags=["success:Exception"])
            return util._error_response('Incorrect format', _INTERFACE_VERSION)
        else:
            try:
                #Retrieve individual values from xqueue body and header.
                prompt = util._value_or_default(body['grader_payload']['prompt'], "")
                rubric = util._value_or_default(body['grader_payload']['rubric'], "")
                student_id = util._value_or_default(body['student_info']['anonymous_student_id'])
                location = util._value_or_default(body['grader_payload']['location'])
                course_id = util._value_or_default(body['grader_payload']['course_id'])
                problem_id = util._value_or_default(body['grader_payload']['problem_id'], location)
                grader_settings = util._value_or_default(body['grader_payload']['grader_settings'], "")
                student_response = util._value_or_default(body['student_response'])
                xqueue_submission_id = util._value_or_default(header['submission_id'])
                xqueue_submission_key = util._value_or_default(header['submission_key'])
                state_code = SubmissionState.waiting_to_be_graded
                xqueue_queue_name = util._value_or_default(header["queue_name"])
                max_score = util._value_or_default(body['max_score'])

                submission_time_string = util._value_or_default(body['student_info']['submission_time'])
                student_submission_time = datetime.strptime(submission_time_string, "%Y%m%d%H%M%S")

                #Create submission object
                sub, created = Submission.objects.get_or_create(
                    prompt=prompt,
                    rubric=rubric,
                    student_id=student_id,
                    problem_id=problem_id,
                    state=state_code,
                    student_response=student_response,
                    student_submission_time=student_submission_time,
                    xqueue_submission_id=xqueue_submission_id,
                    xqueue_submission_key=xqueue_submission_key,
                    xqueue_queue_name=xqueue_queue_name,
                    location=location,
                    course_id=course_id,
                    max_score=max_score,
                    grader_settings=grader_settings,
                )

            except Exception as err:
                xqueue_submission_id = util._value_or_default(header['submission_id'])
                xqueue_submission_key = util._value_or_default(header['submission_key'])
                log.error(
                    "Error creating submission and adding to database: sender: {0}, submission_id: {1}, submission_key: {2}".format(
                        util.get_request_ip(request),
                        xqueue_submission_id,
                        xqueue_submission_key,
                    ))

                statsd.increment("open_ended_assessment.grading_controller.controller.xqueue_interface.submit",
                    tags=["success:Exception"])

                return util._error_response('Unable to create submission.', _INTERFACE_VERSION)

            #Handle submission and write to db
            success = handle_submission(sub)
            statsd.increment("open_ended_assessment.grading_controller.controller.xqueue_interface.submit",
                tags=[
                    "success:{0}".format(success),
                    "location:{0}".format(sub.location),
                    "course_id:{0}".format(course_id),
                ])

            if not success:
                return util._error_response("Failed to handle submission.", _INTERFACE_VERSION)

            return util._success_response({'message': "Saved successfully."}, _INTERFACE_VERSION)


def handle_submission(sub):
    """
    Handles a new submission.  Decides what the next grader should be and saves it.
    Input:
        sub - A Submission object from controller.models

    Output:
        True/False status code
    """
    try:
        #Run some basic sanity checks on submission
        sub.next_grader_type = "BC"
        sub.save()
        timing_functions.initialize_timing(sub.id)
        success, check_dict = basic_check_util.simple_quality_check(sub.student_response)
        if not success:
            log.exception("could not run basic checks on {0}".format(sub.student_response))
            return False

        #add additional tags needed to create a grader object
        check_dict = grader_util.add_additional_tags_to_dict(check_dict, sub.id)

        #Create and handle the grader, and return
        grader_util.create_and_handle_grader_object(check_dict)

        #If the checks result in a score of 0 (out of 1), then the submission fails basic sanity checks
        #Return to student and don't process further
        if check_dict['score'] == 0:
            return True
        else:
            sub.state=SubmissionState.waiting_to_be_graded

        #Assign whether grader should be ML or IN based on number of graded examples.
        subs_graded_by_instructor, subs_pending_instructor = staff_grading_util.count_submissions_graded_and_pending_instructor(
            sub.location)

        #TODO: abstract out logic for assigning which grader to go with.
        grader_settings_path = os.path.join(settings.GRADER_SETTINGS_DIRECTORY, sub.grader_settings)
        grader_settings = grader_util.get_grader_settings(grader_settings_path)
        if grader_settings['grader_type'] == "ML":
            if((subs_graded_by_instructor + subs_pending_instructor) >= settings.MIN_TO_USE_ML):
                sub.next_grader_type = "ML"
            else:
                sub.next_grader_type = "IN"
        elif grader_settings['grader_type'] == "PE":
            #Ensures that there will be some calibration essays before peer grading begins!
            #Calibration essays can be added using command line utility, or through normal instructor grading.
            if((subs_graded_by_instructor + subs_pending_instructor) >= settings.MIN_TO_USE_PEER):
                sub.next_grader_type = "PE"
            else:
                sub.next_grader_type = "IN"
        else:
            log.exception("Invalid grader type specified in settings file.")
            return False

        sub.save()
        log.debug("Submission object created successfully!")

    except:
        log.exception("Submission creation failed!")
        return False

    return True


def _is_valid_reply(external_reply):
    '''
    Check if external reply is in the right format
        1) Presence of 'xqueue_header' and 'xqueue_body'
        2) Presence of specific metadata in 'xqueue_header'
            ['submission_id', 'submission_key']

    Returns:
        is_valid:       Flag indicating success (Boolean)
        header :        header of the queue item
        body:           body of the queue item
    '''
    fail = (False, -1, '')

    try:
        header = json.loads(external_reply['xqueue_header'])
        body = json.loads(external_reply['xqueue_body'])
    except KeyError:
        log.debug("Cannot load header or body.")
        return fail

    if not isinstance(header, dict) or not isinstance(body, dict):
        return fail

    for tag in ['submission_id', 'submission_key', 'queue_name']:
        if not header.has_key(tag):
            log.debug("{0} not found in header".format(tag))
            return fail

    for tag in ['grader_payload', 'student_response', 'student_info']:
        if not body.has_key(tag):
            log.debug("{0} not found in body".format(tag))
            return fail

    try:
        body['grader_payload'] = json.loads(body['grader_payload'])
        body['student_info'] = json.loads(body['student_info'])
    except:
        log.debug("Cannot load payload or info.")
        return fail

    return True, header, body

