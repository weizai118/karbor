#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Implementation of SQLAlchemy backend."""

import functools
import re
import sys
import threading
import time
import uuid

from oslo_config import cfg
from oslo_db import api as oslo_db_api
from oslo_db import exception as db_exc
from oslo_db import options
from oslo_db.sqlalchemy import session as db_session
from oslo_log import log as logging
from oslo_utils import timeutils
from sqlalchemy.orm import joinedload
from sqlalchemy.sql.expression import literal_column
from sqlalchemy.sql import func

from smaug.db.sqlalchemy import models
from smaug import exception
from smaug.i18n import _, _LW


CONF = cfg.CONF
LOG = logging.getLogger(__name__)

options.set_defaults(CONF, connection='sqlite:///$state_path/smaug.sqlite')

_LOCK = threading.Lock()
_FACADE = None
_GET_METHODS = {}


def _create_facade_lazily():
    global _LOCK
    with _LOCK:
        global _FACADE
        if _FACADE is None:
            _FACADE = db_session.EngineFacade(
                CONF.database.connection,
                **dict(CONF.database)
            )

        return _FACADE


def get_engine():
    facade = _create_facade_lazily()
    return facade.get_engine()


def get_session(**kwargs):
    facade = _create_facade_lazily()
    return facade.get_session(**kwargs)


def dispose_engine():
    get_engine().dispose()

_DEFAULT_QUOTA_NAME = 'default'


def get_backend():
    """The backend is this module itself."""

    return sys.modules[__name__]


def is_admin_context(context):
    """Indicates if the request context is an administrator."""
    if not context:
        LOG.warning(_LW('Use of empty request context is deprecated'),
                    DeprecationWarning)
        raise Exception('die')
    return context.is_admin


def is_user_context(context):
    """Indicates if the request context is a normal user."""
    if not context:
        return False
    if context.is_admin:
        return False
    if not context.user_id or not context.project_id:
        return False
    return True


def authorize_project_context(context, project_id):
    """Ensures a request has permission to access the given project."""
    if is_user_context(context):
        if not context.project_id:
            raise exception.NotAuthorized()
        elif context.project_id != project_id:
            raise exception.NotAuthorized()


def authorize_user_context(context, user_id):
    """Ensures a request has permission to access the given user."""
    if is_user_context(context):
        if not context.user_id:
            raise exception.NotAuthorized()
        elif context.user_id != user_id:
            raise exception.NotAuthorized()


def require_admin_context(f):
    """Decorator to require admin request context.

    The first argument to the wrapped function must be the context.

    """

    def wrapper(*args, **kwargs):
        if not is_admin_context(args[0]):
            raise exception.AdminRequired()
        return f(*args, **kwargs)
    return wrapper


def require_context(f):
    """Decorator to require *any* user or admin context.

    This does no authorization for user or project access matching, see
    :py:func:`authorize_project_context` and
    :py:func:`authorize_user_context`.

    The first argument to the wrapped function must be the context.

    """

    def wrapper(*args, **kwargs):
        if not is_admin_context(args[0]) and not is_user_context(args[0]):
            raise exception.NotAuthorized()
        return f(*args, **kwargs)
    return wrapper


def require_plan_exists(f):
    """Decorator to require the specified plan to exist.

    Requires the wrapped function to use context and plan_id as
    their first two arguments.
    """
    @functools.wraps(f)
    def wrapper(context, plan_id, *args, **kwargs):
        plan_get(context, plan_id)
        return f(context, plan_id, *args, **kwargs)
    return wrapper


def _retry_on_deadlock(f):
    """Decorator to retry a DB API call if Deadlock was received."""
    @functools.wraps(f)
    def wrapped(*args, **kwargs):
        while True:
            try:
                return f(*args, **kwargs)
            except db_exc.DBDeadlock:
                LOG.warning(_LW("Deadlock detected when running "
                                "'%(func_name)s': Retrying..."),
                            dict(func_name=f.__name__))
                # Retry!
                time.sleep(0.5)
                continue
    functools.update_wrapper(wrapped, f)
    return wrapped


def model_query(context, *args, **kwargs):
    """Query helper that accounts for context's `read_deleted` field.

    :param context: context to query under
    :param session: if present, the session to use
    :param read_deleted: if present, overrides context's read_deleted field.
    :param project_only: if present and context is user-type, then restrict
            query to match the context's project_id.
    """
    session = kwargs.get('session') or get_session()
    read_deleted = kwargs.get('read_deleted') or context.read_deleted
    project_only = kwargs.get('project_only')

    query = session.query(*args)
    if read_deleted == 'no':
        query = query.filter_by(deleted=False)
    elif read_deleted == 'yes':
        pass  # omit the filter to include deleted and active
    elif read_deleted == 'only':
        query = query.filter_by(deleted=True)
    else:
        raise Exception(
            _("Unrecognized read_deleted value '%s'") % read_deleted)

    if project_only and is_user_context(context):
        query = query.filter_by(project_id=context.project_id)

    return query


@require_admin_context
def service_destroy(context, service_id):
    session = get_session()
    with session.begin():
        service_ref = _service_get(context, service_id, session=session)
        service_ref.delete(session=session)


@require_admin_context
def _service_get(context, service_id, session=None):
    result = model_query(
        context,
        models.Service,
        session=session).\
        filter_by(id=service_id).\
        first()
    if not result:
        raise exception.ServiceNotFound(service_id=service_id)

    return result


@require_admin_context
def service_get(context, service_id):
    return _service_get(context, service_id)


@require_admin_context
def service_get_all(context, disabled=None):
    query = model_query(context, models.Service)

    if disabled is not None:
        query = query.filter_by(disabled=disabled)

    return query.all()


@require_admin_context
def service_get_all_by_topic(context, topic, disabled=None):
    query = model_query(
        context, models.Service, read_deleted="no").\
        filter_by(topic=topic)

    if disabled is not None:
        query = query.filter_by(disabled=disabled)

    return query.all()


@require_admin_context
def service_get_by_host_and_topic(context, host, topic):
    result = model_query(
        context, models.Service, read_deleted="no").\
        filter_by(disabled=False).\
        filter_by(host=host).\
        filter_by(topic=topic).\
        first()
    if not result:
        raise exception.ServiceNotFound(service_id=None)
    return result


@require_admin_context
def _service_get_all_topic_subquery(context, session, topic, subq, label):
    sort_value = getattr(subq.c, label)
    return model_query(context, models.Service,
                       func.coalesce(sort_value, 0),
                       session=session, read_deleted="no").\
        filter_by(topic=topic).\
        filter_by(disabled=False).\
        outerjoin((subq, models.Service.host == subq.c.host)).\
        order_by(sort_value).\
        all()


@require_admin_context
def service_get_by_args(context, host, binary):
    results = model_query(context, models.Service).\
        filter_by(host=host).\
        filter_by(binary=binary).\
        all()

    for result in results:
        if host == result['host']:
            return result

    raise exception.HostBinaryNotFound(host=host, binary=binary)


@require_admin_context
def service_create(context, values):
    service_ref = models.Service()
    service_ref.update(values)
    if not CONF.enable_new_services:
        service_ref.disabled = True

    session = get_session()
    with session.begin():
        service_ref.save(session)
        return service_ref


@require_admin_context
def service_update(context, service_id, values):
    session = get_session()
    with session.begin():
        service_ref = _service_get(context, service_id, session=session)
        if ('disabled' in values):
            service_ref['modified_at'] = timeutils.utcnow()
            service_ref['updated_at'] = literal_column('updated_at')
        service_ref.update(values)
        return service_ref


def _get_get_method(model):
    # General conversion
    # Convert camel cased model name to snake format
    s = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', model.__name__)
    # Get method must be snake formatted model name concatenated with _get
    method_name = re.sub('([a-z0-9])([A-Z])', r'\1_\2', s).lower() + '_get'
    return globals().get(method_name)


@require_context
def get_by_id(context, model, id, *args, **kwargs):
    # Add get method to cache dictionary if it's not already there
    if not _GET_METHODS.get(model):
        _GET_METHODS[model] = _get_get_method(model)

    return _GET_METHODS[model](context, id, *args, **kwargs)


###################


def trigger_get(context, id):
    return _trigger_get(context, id)


def _trigger_get(context, id, session=None):
    result = model_query(context, models.Trigger,
                         session=session).filter_by(id=id)
    result = result.first()

    if not result:
        raise exception.TriggerNotFound(id=id)

    return result


def trigger_create(context, values):
    trigger_ref = models.Trigger()
    trigger_ref.update(values)
    trigger_ref.save(get_session())
    return trigger_ref


@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
def trigger_update(context, id, values):
    """Update the Trigger record with the most recent data."""

    session = get_session()
    with session.begin():
        trigger_ref = _trigger_get(context, id, session=session)
        trigger_ref.update(values)
        trigger_ref.save(session)
    return trigger_ref


def trigger_delete(context, id):
    """Delete a Trigger record."""

    session = get_session()
    with session.begin():
        trigger_ref = _trigger_get(context, id, session=session)
        trigger_ref.delete(session=session)


###################


def scheduled_operation_get(context, id, columns_to_join=[]):
    return _scheduled_operation_get(context, id,
                                    columns_to_join=columns_to_join)


def _scheduled_operation_get(context, id, columns_to_join=[], session=None):
    query = model_query(context, models.ScheduledOperation,
                        session=session).filter_by(id=id)

    if columns_to_join and 'trigger' in columns_to_join:
        query = query.options(joinedload('trigger'))

    result = query.first()
    if not result:
        raise exception.ScheduledOperationNotFound(id=id)

    return result


def scheduled_operation_create(context, values):
    operation_ref = models.ScheduledOperation()
    operation_ref.update(values)
    operation_ref.save(get_session())
    return operation_ref


@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
def scheduled_operation_update(context, id, values):
    """Update the ScheduledOperation record with the most recent data."""

    session = get_session()
    with session.begin():
        operation_ref = _scheduled_operation_get(context, id,
                                                 session=session)
        operation_ref.update(values)
        operation_ref.save(session)
    return operation_ref


def scheduled_operation_delete(context, id):
    """Delete a ScheduledOperation record."""

    session = get_session()
    with session.begin():
        operation_ref = _scheduled_operation_get(context, id,
                                                 session=session)
        session.delete(operation_ref)
        session.flush()


###################


def scheduled_operation_state_get(context, operation_id):
    return _scheduled_operation_state_get(context, operation_id)


def _scheduled_operation_state_get(context, operation_id, session=None):
    result = model_query(context, models.ScheduledOperationState,
                         session=session).filter_by(operation_id=operation_id)
    result = result.first()
    if not result:
        raise exception.ScheduledOperationStateNotFound(op_id=operation_id)

    return result


def scheduled_operation_state_create(context, values):
    state_ref = models.ScheduledOperationState()
    state_ref.update(values)
    state_ref.save(get_session())
    return state_ref


@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
def scheduled_operation_state_update(context, operation_id, values):
    """Update the ScheduledOperationState record with the most recent data."""

    session = get_session()
    with session.begin():
        state_ref = _scheduled_operation_state_get(context, operation_id,
                                                   session=session)
        state_ref.update(values)
        state_ref.save(session)
    return state_ref


def scheduled_operation_state_delete(context, operation_id):
    """Delete a ScheduledOperationState record."""

    session = get_session()
    with session.begin():
        state_ref = _scheduled_operation_state_get(context, operation_id,
                                                   session=session)
        state_ref.delete(session=session)


###################


def scheduled_operation_log_get(context, log_id):
    return _scheduled_operation_log_get(context, log_id)


def _scheduled_operation_log_get(context, log_id, session=None):
    result = model_query(context, models.ScheduledOperationLog,
                         session=session).filter_by(id=log_id).first()

    if not result:
        raise exception.ScheduledOperationLogNotFound(log_id=log_id)

    return result


def scheduled_operation_log_create(context, values):
    log_ref = models.ScheduledOperationLog()
    log_ref.update(values)
    log_ref.save(get_session())
    return log_ref


@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
def scheduled_operation_log_update(context, log_id, values):
    """Update the ScheduledOperationLog record with the most recent data."""

    session = get_session()
    with session.begin():
        log_ref = _scheduled_operation_log_get(context, log_id,
                                               session=session)
        log_ref.update(values)
        log_ref.save(session)
    return log_ref


def scheduled_operation_log_delete(context, log_id):
    """Delete a ScheduledOperationLog record."""

    session = get_session()
    with session.begin():
        log_ref = _scheduled_operation_log_get(context, log_id,
                                               session=session)
        session.delete(log_ref)
        session.flush()


def _resource_refs(resource_list, meta_class):
    resource_refs = []
    if resource_list:
        for resource in resource_list:
            resource_ref = meta_class()
            resource_ref['resource_id'] = resource['id']
            resource_ref['resource_type'] = resource['type']
            resource_refs.append(resource_ref)
    return resource_refs


@require_context
def _plan_get_query(context, session=None, project_only=False,
                    joined_load=True):
    """Get the query to retrieve the plan.

    :param context: the context used to run the method _plan_get_query
    :param session: the session to use
    :param project_only: the boolean used to decide whether to query the
                         plan in the current project or all projects
    :param joined_load: the boolean used to decide whether the query loads
                        the other models, which join the plan model in
                        the database.
    :returns: updated query or None
    """
    query = model_query(context, models.Plan, session=session,
                        project_only=project_only)
    if joined_load:
        query = query.options(joinedload('resources'))
    return query


def _plan_resources_get_query(context, plan_id, model, session=None):
    return model_query(context, model, session=session, read_deleted="no").\
        filter_by(plan_id=plan_id)


@require_context
def _resource_create(context, values):
    resource_ref = models.Resource()
    resource_ref.update(values)
    session = get_session()
    with session.begin():
        resource_ref.save(session)
        return resource_ref


@require_context
def _plan_resources_update(context, plan_id, resources, session=None):
    session = session or get_session()
    now = timeutils.utcnow()
    with session.begin():
        model_query(context, models.Resource, session=session).\
            filter_by(plan_id=plan_id).\
            update({'deleted': True,
                    'deleted_at': now,
                    'updated_at': literal_column('updated_at')})
    resources_list = []
    for resource in resources:
        resource['plan_id'] = plan_id
        resource['resource_id'] = resource.pop('id')
        resource['resource_type'] = resource.pop('type')
        resource_ref = _resource_create(context, resource)
        resources_list.append(resource_ref)

    return resources_list


@require_context
def _plan_get(context, plan_id, session=None, joined_load=True):
    result = _plan_get_query(context, session=session, project_only=True,
                             joined_load=joined_load)
    result = result.filter_by(id=plan_id).first()

    if not result:
        raise exception.PlanNotFound(plan_id=plan_id)

    return result


@require_context
def plan_create(context, values):
    values['resources'] = _resource_refs(values.get('resources'),
                                         models.Resource)

    plan_ref = models.Plan()
    if not values.get('id'):
        values['id'] = str(uuid.uuid4())
    plan_ref.update(values)

    session = get_session()
    with session.begin():
        session.add(plan_ref)

    return _plan_get(context, values['id'], session=session)


@require_context
def plan_get(context, plan_id):
    return _plan_get(context, plan_id)


@require_admin_context
@_retry_on_deadlock
def plan_destroy(context, plan_id):
    session = get_session()
    now = timeutils.utcnow()
    with session.begin():
        model_query(context, models.Plan, session=session).\
            filter_by(id=plan_id).\
            update({'deleted': True,
                    'deleted_at': now,
                    'updated_at': literal_column('updated_at')})
        model_query(context, models.Resource, session=session).\
            filter_by(plan_id=plan_id).\
            update({'deleted': True,
                    'deleted_at': now,
                    'updated_at': literal_column('updated_at')})


@require_context
@require_plan_exists
def plan_update(context, plan_id, values):
    session = get_session()
    with session.begin():
        resources = values.get('resources')
        if resources is not None:
            _plan_resources_update(context,
                                   plan_id,
                                   values.pop('resources'),
                                   session=session)

        plan_ref = _plan_get(context, plan_id, session=session)
        plan_ref.update(values)

        return plan_ref


@require_context
@require_plan_exists
@_retry_on_deadlock
def plan_resources_update(context, plan_id, resources):
    return _plan_resources_update(context,
                                  plan_id,
                                  resources)
