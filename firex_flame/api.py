"""
Flask API module for interacting with celery tasks.
"""

import logging

from flask import jsonify

from firex_flame.flame_helper import wait_until

from firex_flame.event_aggregator import slim_tasks_by_uuid, INCOMPLETE_STATES

logger = logging.getLogger(__name__)


def create_socketio_task_api(sio_server, event_aggregator, run_metadata):

    @sio_server.on('send-graph-state')
    def emit_frontend_tasks_by_uuid(sid):
        """ Send 'slim' fields for all tasks. This allows visualization of the graph."""
        sio_server.emit('graph-state', slim_tasks_by_uuid(event_aggregator.tasks_by_uuid), room=sid)

    @sio_server.on('send-graph-fields')
    def emit_task_fields_by_uuid(sid, fields):
        """ Send the requested fields for all tasks."""
        response = {uuid: {f: v for f, v in task.items() if f in fields}
                    for uuid, task in event_aggregator.tasks_by_uuid.items()}
        sio_server.emit('graph-fields', response, room=sid)

    @sio_server.on('send-run-metadata')
    def emit_run_metadata(sid):
        """ Get static run-level data."""
        response = {
            'uid': run_metadata['uid'],
            'logs_dir': run_metadata['logs_dir'],
            'root_uuid': event_aggregator.root_uuid,
            'chain': run_metadata['chain'],
            'centralServer': run_metadata['central_server'],
            'centralServerUiPath': None,  # TODO: propagate this.
            'central_documentation_url': run_metadata['central_documentation_url'],
        }
        sio_server.emit('run-metadata', response, room=sid)

    @sio_server.on('send-task-details')
    def emit_detailed_tasks(sid, uuids):
        """ Get all fields for requested task UUIDs.
        Arguments:
            sid: The session ID to emit to.
            uuids (str or list of str): The uuid of the desired task to get details for.
        """
        if isinstance(uuids, str):
            uuid = uuids
            response = event_aggregator.tasks_by_uuid.get(uuid, None)
            sio_server.emit('task-details-' + uuid, response, room=sid)
        else:
            if not isinstance(uuids, list):
                response = []
            else:
                response = [event_aggregator.tasks_by_uuid.get(u, None) for u in uuids]
            sio_server.emit('task-details', response, room=sid)


def create_rest_task_api(web_app, tasks):

    @web_app.route('/api/tasks')
    def all_tasks_by_uuid():
        return jsonify(slim_tasks_by_uuid(tasks))


def create_revoke_api(sio_server, celery_app, tasks):

    @sio_server.on('revoke-task')
    def socket_revoke_task(sid, uuid):
        logger.info("Received request to revoke %s" % uuid)
        revoked = _revoke_task(uuid)
        response_event = 'revoke-success' if revoked else 'revoke-failed'
        sio_server.emit(response_event, room=sid)

    def _wait_until_task_complete(task, timeout, sleep_for=1):
        wait_until(lambda t: t['state'] not in INCOMPLETE_STATES, timeout, sleep_for, task)

    def _revoke_task(uuid):
        if uuid not in tasks:
            return False

        # Get the task instance
        task = tasks[uuid]

        # Try to revoke the task
        if task['state'] in INCOMPLETE_STATES:
            celery_app.control.revoke(uuid, terminate=True)
            logger.info("Submitted revoke to celery for: %s" % uuid)
        else:
            logger.info("Task %s already in terminal state %s" % (uuid, task['state']))

        # Wait for the task to become revoked
        revoke_timeout = 20
        _wait_until_task_complete(task, timeout=revoke_timeout)

        task_runstate = task['state']
        revoked = task_runstate == 'task-revoked'
        if not revoked:
            logger.warning("Failed to revoke task: waited %s sec and runstate is currently %s"
                           % (revoke_timeout, task_runstate))

        return revoked  # If the task was successfully revoked, return true
