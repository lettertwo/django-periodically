from .models import ExecutionRecord
from datetime import datetime
from .signals import task_complete
import logging
import sys


# FIXME: We really need to clarify our use of the word "scheduled." Does it mean scheduled with the backend (as in schedule_task) or scheduled to run (as in run_scheduled_tasks)


class BaseBackend(object):
    """
    Keeps a schedule of periodic tasks.
    """
    _schedules = []

    @property
    def logger(self):
        return logging.getLogger('periodically') # TODO: Further namespace logger?
    
    @property
    def tasks(self):
        """A list of the tasks scheduled with this backend."""
        return set([task for task, schedule in self._schedules])
    
    def schedule_task(self, task, schedule):
        """
        Schedules a periodic task.
        """
        # Don't add the same one twice.
        for t, s in self._schedules:
            if t.task_id == task.task_id and s == schedule:
                return
        
        task_id = task.task_id
        self.logger.info('Scheduling task %s to run on schedule %s' % (task_id, schedule))
        self._schedules.append((task, schedule))
        
        # Subscribe to the task_complete signal. We do this when the task
        # is scheduled (instead of when it runs) so that if you kill Django
        # with unfinished tasks still running, they will be able to
        # complete. (For example, whether a task is completed might be
        # determined by polling a web service. When Django restarts, the
        # polling could start again and the task would be completed.)
        if not getattr(task, 'is_blocking', True):
            task_complete.connect(self._create_receiver(task.__class__), sender=task.__class__, dispatch_uid=task_id)
    
    def run_scheduled_tasks(self, tasks=None, fake=None):
        """
        Runs any scheduled periodic tasks and ends any tasks that have exceeded
        their timeout. The optional <code>tasks</code> argument allows you to
        run only a subset of the registered tasks.
        """
        self._run_tasks(tasks, fake, False)

    def run_tasks(self, tasks=None, fake=None):
        """
        Run tasks regardless of whether they are scheduled.
        """
        self._run_tasks(tasks, fake, True)
    
    def _run_tasks(self, tasks=None, fake=None, force=False):
        # Verify that the provided tasks actually exist.
        if tasks:
            for task in tasks:
                if task.task_id not in set([task.task_id for task in self.tasks]):
                    raise Exception('%s is not registered with this backend.' % task)
        
        registered_task_ids = [task.task_id for task in tasks]
        
        for task, schedule in self._schedules:
            if not tasks or task.task_id in registered_task_ids:
                
                # Cancel the task if it's timed out.
                # FIXME: This should only be called once per task (no matter how many times it's scheduled).
                self.check_timeout(task)
            
                # Run the task if it's due (or past due).
                if force or schedule.get_scheduled_time(task) <= datetime.now():
                    previous_record = schedule.get_previous_record(task)
                    fake_task = fake or fake is None and previous_record is None
                
                    # If we're forcing the task, use the previous scheduled
                    # time. That way we don't put off any upcoming tasks whose
                    # schedule's get_scheduled_time method relies on the time
                    # of the previous execution.
                    scheduled_time = previous_record.scheduled_time if force and previous_record else None

                    if fake_task:
                        self.fake_task(task, schedule, scheduled_time)
                    else:
                        self.run_task(task, schedule, scheduled_time)

    def check_timeout(self, task):
        from .settings import DEFAULT_TIMEOUT
        for record in ExecutionRecord.objects.filter(task_id=task.task_id, end_time__isnull=True):
            timeout = getattr(task, 'timeout', DEFAULT_TIMEOUT)
            running_time = datetime.now() - record.start_time
            if running_time > timeout:
                extra = {
                    'level': logging.ERROR,
                    'msg': 'Task timed out after %s.' % running_time,
                }
                self.complete_task(task, extra=extra)
    
    def check_timeouts(self):
        """
        Checks to see whether any scheduled tasks have timed out and handles
        those that have.
        """
        for task in self.tasks:
            self.check_timeout(task)
    
    def fake_task(self, task, schedule, scheduled_time=None):
        # TODO: Do we need both of these?
        print 'Faking periodic task "%s"' % task.task_id
        self.logger.info('Faking periodic task "%s"' % task.task_id)

        if scheduled_time is None:
            scheduled_time = schedule.get_scheduled_time(task)
        
        # Create the log for this execution.
        log = ExecutionRecord.objects.create(
            task_id=task.task_id,
            schedule_id=schedule.__hash__(),
            scheduled_time=scheduled_time,
            start_time=datetime.now(),
            end_time=datetime.now(),
            is_fake=True,)
    
    def run_task(self, task, schedule, scheduled_time=None):
        """
        Runs the provided task. This method is provided as a convenience to
        subclasses so that they do not have to implement all of the extra stuff
        that goes with running a single task--for example, retries, failure
        emails, etc. If you want, your subclass's run_scheduled_tasks method
        can call task.run() directly (avoiding this method), but it is highly
        discouraged.
        """
        # TODO: Do we need both of these?
        print 'Running periodic task "%s"' % task.task_id
        self.logger.info('Running periodic task "%s"' % task.task_id)
        
        
        if scheduled_time is None:
            scheduled_time = schedule.get_scheduled_time(task)
        
        # Create the log for this execution.
        log = ExecutionRecord.objects.create(
            task_id=task.task_id,
            schedule_id=schedule.__hash__(),
            scheduled_time=scheduled_time,
            start_time=datetime.now(),
            end_time=None,)

        # Run the task.
        try:
            task.run()
        except Exception, err:
            extra = {
                'level': logging.ERROR,
                'msg': str(err),
                'exc_info': sys.exc_info(),
            }
        else:
            extra = None

        if extra is not None or getattr(task, 'is_blocking', True):
            self.complete_task(task, extra=extra)
    
    def _create_receiver(self, sender):
        def receiver(task, extra=None):
            task_complete.disconnect(receiver, sender, dispatch_uid=task.task_id)
            self.complete_task(task, extra=extra)
        return receiver
    
    def complete_task(self, task, extra=None):
        """
        Marks a task as complete and performs other post-completion tasks. The
        <code>extra</code> argument is a dictionary of values to be passed to
        <code>Logger.log()</code> as keyword args.
        """
        if extra is not None:
            self.logger.log(**extra)
            completed_successfully = extra.get('level', logging.ERROR) != logging.ERROR
        else:
            completed_successfully = True
            
        record = ExecutionRecord.objects.filter(task_id=task.task_id, end_time=None).order_by('-start_time')[0]
        record.end_time = datetime.now()
        record.completed_successfully = completed_successfully
        record.save()

        # TODO: Retries.


class DefaultBackend(BaseBackend):
    """
    A backend that only runs tasks when explicitly told to (i.e. when its
    `run_scheduled_tasks()` method is invoked).
    """
    pass
