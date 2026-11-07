import gc
import tracemalloc
import unittest

import adk_runtime_legacy as legacy
import adk_runtime_optimized as optimized
from referee_adk_bench import peak_memory, session_measure, summary
from workload import digest, exercise


class MeasurementPipelineTests(unittest.TestCase):
    def test_clone_preserves_cycles_and_aliases_without_reusing_input(self):
        original = {'items': []}
        original['items'].append(original)
        copied = optimized._clone(original)
        self.assertIsNot(copied, original)
        self.assertIs(copied['items'][0], copied)
        copied['items'].append('caller edit')
        self.assertEqual(len(original['items']), 1)

    def test_summary_keeps_samples_and_interpolates_no_new_values(self):
        values = [9, 1, 5, 3, 7]
        result = summary(values)
        self.assertEqual(result['median'], 5)
        self.assertEqual((result['min'], result['max'], result['p10'], result['p90']), (1, 9, 1, 7))
        self.assertEqual(values, [9, 1, 5, 3, 7])

    def test_real_workload_measurements_gate_digest_and_restore_process_state(self):
        expected = digest(exercise(legacy, 5, True))
        enabled = gc.isenabled()
        for module in (legacy, optimized):
            self.assertGreaterEqual(session_measure(module, 5, True, expected), 0)
            self.assertEqual(gc.isenabled(), enabled)
            self.assertGreater(peak_memory(module, 5, True, expected), 0)
            self.assertFalse(tracemalloc.is_tracing())
            with self.assertRaisesRegex(AssertionError, 'differs'):
                session_measure(module, 5, True, 'invalid digest')
            self.assertEqual(gc.isenabled(), enabled)
