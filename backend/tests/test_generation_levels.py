from backend.tests.test_worker import WorkerCase
from backend.tests.test_autopilot import FIT_PLAIN

class GenerationLevelTests(WorkerCase):
    def test_default_and_explicit_levels_do_not_include_archived(self):
        junior = self.add_job('Junior', status='ready', fit={**FIT_PLAIN, 'level':'junior'})
        unknown = self.add_job('Unknown', status='ready')
        mid = self.add_job('Mid', status='ready', fit={**FIT_PLAIN, 'level':'mid'})
        senior = self.add_job('Senior', status='ready', fit={**FIT_PLAIN, 'level':'senior'})
        archived = self.add_job('Archived', status='screened_out', fit={**FIT_PLAIN, 'level':'senior'})
        self.assertEqual(self.worker.generate_ready(), 2)
        self.assertEqual(self.status_of(junior), 'queued')
        self.assertEqual(self.status_of(unknown), 'queued')
        self.assertEqual(self.status_of(mid), 'ready')
        self.assertEqual(self.status_of(senior), 'ready')
        self.assertEqual(self.worker.generate_ready('mid'), 1)
        self.assertEqual(self.status_of(mid), 'queued')
        self.assertEqual(self.status_of(senior), 'ready')
        self.assertEqual(self.worker.generate_ready('senior'), 1)
        self.assertEqual(self.status_of(senior), 'queued')
        self.assertEqual(self.status_of(archived), 'screened_out')
