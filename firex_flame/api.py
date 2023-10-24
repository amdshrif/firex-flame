"""
Flask API module for interacting with celery tasks.
"""

import logging

from flask import jsonify, request
from gevent import spawn, sleep
from socket import gethostname
import os
import subprocess
import paramiko
import time

from firex_flame.flame_helper import wait_until, query_full_tasks, REVOKE_REASON_KEY, REVOKE_TIMESTAMP_KEY
from firex_flame.event_aggregator import slim_tasks_by_uuid, INCOMPLETE_STATES, FlameEventAggregator
from firex_flame.controller import FlameAppController


logger = logging.getLogger(__name__)

subprocess_dict = {}


def _run_metadata_to_api_model(run_metadata, root_uuid):
    return {
        'uid': run_metadata['uid'],
        'logs_dir': run_metadata['logs_dir'],
        'root_uuid': root_uuid,
        'chain': run_metadata['chain'],
        'logs_server': run_metadata['logs_server'],
    }


def _get_task_fields(tasks_by_uuid, fields):
    return {uuid: {f: v for f, v in task.items() if f in fields}
            for uuid, task in tasks_by_uuid.items()}


def poll_channel_readable(channel, timeout=0):
    interval = 0.1
    so_far = 0
    while so_far <= timeout:
        if channel.recv_ready():
            return True
        sleep(interval)
        so_far += interval
    return False


def monitor_file(sio_server, sid, host, filename):
    # To avoid issues with huge log files, we only get the last 50000 lines
    max_lines = 50000

    # helper to send data
    def emit_line_data(data):
        sio_server.emit('file-data', data, room=sid)

    # check if host is localhost or remote - use ssh if remote
    if host in ['127.0.0.1', 'localhost', gethostname()]:
        # Read file locally - output to be sent to requesting client
        logger.info("Will start monitoring file %s locally" % filename)

        if not os.path.isfile(filename):
            emit_line_data("File %s does not exist." % filename)
            return

        if not os.access(filename, os.R_OK):
            emit_line_data("File %s is not accessible." % filename)
            return

        try:
            proc = subprocess.Popen(['/usr/bin/tail', '-n', str(max_lines), '--follow=name', filename, '2>/dev/null'],
                                    stdout=subprocess.PIPE)
        except Exception:
            logger.warning("Exception raised while trying to spawn subprocess to monitor file: ", exc_info=True)
            return

        # keep track of sub-procs for later cleanup
        subprocess_dict[sid] = proc

        while True:
            line = proc.stdout.readline()
            if not line:
                break
            emit_line_data(line.decode('utf-8'))

    else:
        # spawn ssh to host to tail -f the file - output to be sent to requesting client
        logger.info("Will start monitoring file %s on host %s" % (filename, host))
        # noinspection PyBroadException
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(host, 22, timeout=60, compress=True)

            # Run find to locate file and match perms
            _, stdout, stderr = ssh.exec_command("\\find %s -perm -004" % filename)
            # Wait for command to return
            stdout.channel.recv_exit_status()

            # Read command stdout/err
            res_out = stdout.read()
            res_err = stderr.read()

            # Check our results
            if not res_out or res_out == b'':
                if not res_err or res_err == b'':
                    emit_line_data("ERROR: File access permissions prevent viewing of file.\n")
                else:
                    res_err = res_err.decode('utf-8', 'ignore')
                    if "No such file or directory" in res_err:
                        # File no longer exists on remote host
                        emit_line_data('[Temporary file no longer exists - executed command has completed]\n')
                    else:
                        emit_line_data("ERROR: Unexpected error while checking file existence and permissions: %s" %
                                       res_err)
                return
            else:
                res_out = res_out.decode('utf-8', 'ignore')
                if filename not in res_out:
                    emit_line_data("ERROR: Unexpected output while checking file existence and permissions: %s" % res_out)
                    return

            try:
                # File exists and has open permissions - tail it
                _, stdout, _ = ssh.exec_command("""/bin/bash -c '/usr/bin/tail -n %d --follow=name %s 2>/dev/null' """ %
                                            (max_lines, filename), bufsize=128, get_pty=True)

                # Keep track of all spawned processes to be able to manage them later
                subprocess_dict[sid] = ssh

                # Set read timeout to 1s
                stdout.channel.settimeout(1)

                # local helper function
                def get_data_chunk():
                    from socket import timeout
                    data_chunk = ''
                    max_chunk_lines = 10000
                    num_chunk_lines = 0
                    end_of_file = False
                    while num_chunk_lines < max_chunk_lines:
                        try:
                            line = stdout.readline()
                            if not line or line == '':
                                # empty line signifies eof
                                end_of_file = True
                                break
                        except timeout:
                            # No more data available within timeout: consider this a full data_chunk to be sent off
                            break
                        else:
                            data_chunk += line
                            num_chunk_lines += 1
                    return data_chunk, num_chunk_lines, end_of_file

                total_num_lines = 0
                eof = False
                while not eof:
                    chunk, num_lines, eof = get_data_chunk()
                    if num_lines:
                        if not total_num_lines:
                            chunk = "[start of data received]\n" + chunk
                        emit_line_data(chunk)
                        total_num_lines += num_lines

                if total_num_lines:
                    emit_line_data('[End of file - program exited]\n')
                    return

            except Exception:
                logger.warning("Exception raised while trying to spawn subprocess to monitor file: ", exc_info=True)

        except Exception as e:
            emit_line_data("ERROR: Spawned subprocess to monitor file failed:\n")
            emit_line_data(str(e))


def term_subproc(sid):
    if sid not in subprocess_dict:
        logger.warning("SID %s not in subprocess list" % sid)
        return

    subproc = subprocess_dict[sid]
    subproc.terminate()
    del subprocess_dict[sid]


def term_all_subprocs():
    for sid in subprocess_dict:
        term_subproc(sid)


def create_socketio_task_api(controller, event_aggregator, run_metadata):

    @controller.sio_server.on('send-graph-state')
    def emit_frontend_tasks_by_uuid(sid, data=None):
        """ Send 'slim' fields for all tasks. This allows visualization of the graph."""
        if data and 'task_queries' in data:
            tasks_to_send = query_full_tasks(event_aggregator.tasks_by_uuid, data['task_queries'])
        else:
            tasks_to_send = slim_tasks_by_uuid(event_aggregator.tasks_by_uuid)
        controller.sio_server.emit('graph-state', tasks_to_send, room=sid)

    @controller.sio_server.on('send-graph-fields')
    def emit_task_fields_by_uuid(sid, fields):
        """ Send the requested fields for all tasks."""
        response = _get_task_fields(event_aggregator.tasks_by_uuid, fields)
        controller.sio_server.emit('graph-fields', response, room=sid)

    @controller.sio_server.on('send-run-metadata')
    def emit_run_metadata(sid):
        """ Get static run-level data."""
        response = _run_metadata_to_api_model(run_metadata, event_aggregator.root_uuid)
        controller.sio_server.emit('run-metadata', response, room=sid)

    @controller.sio_server.on('send-task-details')
    def emit_detailed_tasks(sid, uuids):
        """ Get all fields for requested task UUIDs.
        Arguments:
            sid: The session ID to emit to.
            uuids (str or list of str): The uuid of the desired task to get details for.
        """
        if isinstance(uuids, str):
            uuid = uuids
            response = event_aggregator.tasks_by_uuid.get(uuid, None)
            controller.sio_server.emit('task-details-' + uuid, response, room=sid)
        else:
            if not isinstance(uuids, list):
                response = []
            else:
                response = [event_aggregator.tasks_by_uuid.get(u, None) for u in uuids]
            controller.sio_server.emit('task-details', response, room=sid)

    @controller.sio_server.on('start-listen-file')
    def start_file_monitor(sid, args):
        if 'host' not in args or 'filepath' not in args:
            controller.sio_server.emit(
                'file-line',
                "File monitoring request is missing either host and/or filepath parameters: %s" % args,
                room=sid)
        else:
            spawn(monitor_file, sio_server=controller.sio_server, sid=sid, host=args['host'], filename=args['filepath'])

    @controller.sio_server.on('stop-listen-file')
    def stop_file_monitor(sid):
        term_subproc(sid)

    @controller.sio_server.on('start-listen-task-query')
    def start_listen_task_query(sid, args):
        if 'query_config' not in args:
            logger.error("Received request to start listening to query without config.")
        else:
            controller.add_client_task_query_config(sid, args['query_config'])

    @controller.sio_server.on('disconnect')
    def disconnect(sid):
        controller.remove_client_task_query(sid)


def create_rest_task_api(web_app, event_aggregator, run_metadata):

    @web_app.route('/api/tasks')
    def get_all_tasks_by_uuid():
        return jsonify(slim_tasks_by_uuid(event_aggregator.tasks_by_uuid))

    # TODO: add /api/tasks?uuids=uuid1,uuid2, or POST with request body containing query by uuid
    @web_app.route('/api/tasks/<uuid>')
    def get_task_details(uuid):
        if uuid not in event_aggregator.tasks_by_uuid:
            return '', 404
        # if 'fields' in request['query']['fields']:
        #     return jsonify( _get_task_fields(event_aggregator.tasks_by_uuid, request['query']['fields']))
        # No fields were requested, so send all fields.
        return jsonify(event_aggregator.tasks_by_uuid[uuid])

    @web_app.route('/api/run-metadata')
    def get_run_metadata():
        return jsonify(_run_metadata_to_api_model(run_metadata, event_aggregator.root_uuid))


def _data_from_environ_path(environ, path, default):
    if len(path) == 3:
        if path[0] == 'request' and path[1] == 'headers':
            header_key = path[2]
            return  environ.get(f'HTTP_{header_key.upper()}', default)
    return default

def _data_from_request_path(path):
    if len(path) == 3:
        if path[0] == 'request' and path[1] == 'headers':
            header_key = path[2]
            return request.headers.get(header_key)
    return None

def _uuid_and_reason_from_revoke_data(revoke_data):
    if isinstance(revoke_data, str):
            uuid = revoke_data
            revoke_reason = None
    elif isinstance(revoke_data, dict):
        uuid = revoke_data.get('uuid')
        revoke_reason = revoke_data.get(REVOKE_REASON_KEY)
    else:
        uuid = None
        revoke_reason = None
    return uuid, revoke_reason

def create_revoke_api(
    controller: FlameAppController,
    web_app,
    celery_app,
    authed_user_request_path,
    event_aggregator: FlameEventAggregator,
):
    assert controller.sio_server is not None

    sids_to_details = {}
    NO_USER = 'nouser'

    @controller.sio_server.event
    def connect(sid, environ):
        requester = _data_from_environ_path(
            environ, authed_user_request_path, NO_USER)
        sids_to_details[sid] = {
            'requester': requester,
        }

    @controller.sio_server.event
    def disconnect(sid):
        sids_to_details.pop(sid, None)

    @controller.sio_server.on('revoke-task')
    def socketio_revoke_task(sid, revoke_data):
        uuid, revoke_reason = _uuid_and_reason_from_revoke_data(revoke_data)
        if uuid:
            requester = sids_to_details[sid].get('requester', NO_USER)
            revoked = _revoke_task(uuid, 'SocketIO', requester, revoke_reason)
            response_event = 'revoke-success' if revoked else 'revoke-failed'
        else:
            response_event = 'revoke-failed'
        controller.sio_server.emit(response_event, room=sid)

    def _rest_revoke_task(uuid):
        authed_user = _data_from_request_path(authed_user_request_path)
        revoke_reason = request.args.get(REVOKE_REASON_KEY)
        claimed_user = request.args.get('revoking_user')

        user = authed_user or claimed_user or NO_USER
        revoked = _revoke_task(uuid, 'REST', user, revoke_reason)
        return '', 200 if revoked else 500

    @web_app.route('/api/revoke/<uuid>', methods=['GET', 'POST'])
    def rest_revoke_task(uuid):
        return _rest_revoke_task(uuid)

    @web_app.route('/api/revoke', methods=['GET', 'POST'])
    def rest_revoke_root_task():
        if not event_aggregator.root_uuid:
            return '', 500
        logger.debug(f'Revoking entire run via root task UUID: {event_aggregator.root_uuid}')
        return _rest_revoke_task(event_aggregator.root_uuid)

    def _revoke_task(uuid, type, user, revoke_reason):
        msg = f"Received {type} request to revoke {uuid} from user {user}"
        if revoke_reason:
            msg += f' with reason: {revoke_reason}'
            revoke_reason = revoke_reason[:200] # trim since we'll store this.
        logger.info(msg)
        if uuid not in event_aggregator.tasks_by_uuid:
            return False

        prev_revoked_data = None
        task = event_aggregator.tasks_by_uuid[uuid]
        if task['state'] in INCOMPLETE_STATES:
            run_revoked = event_aggregator.root_uuid == uuid
            if run_revoked:
                prev_revoked_data = controller.get_revoke_data()
                controller.update_revoke_reason(revoke_reason)
            celery_app.control.revoke(uuid, terminate=True)
            logger.info(f"Submitted revoke to celery for: {uuid}")
        else:
            logger.info(f"Task {uuid} already in terminal state {task['state']}")

        # Wait for the task to become revoked
        revoke_timeout = 10

        revoked = wait_until(lambda: task.get('was_revoked'), timeout=revoke_timeout, sleep_for=1)
        if not revoked:
            logger.warning(
                f"Failed to revoke task: waited {revoke_timeout} sec and runstate is currently {task['state']}")
            if prev_revoked_data:
                # restore the previous revoke data since the revoke has appeared to fail.
                controller.dump_updated_metadata(prev_revoked_data)
        else:
            logger.debug(f"Successfully revoked task {uuid}.")

        return revoked  # If the task was successfully revoked, return true
