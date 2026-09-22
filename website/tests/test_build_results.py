import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile
from copy import deepcopy
from unittest.mock import patch

from build_results import SOURCE_HASHES, build, build_complete, project, project_complete, summarize_tests


class ResultExportTests(unittest.TestCase):
    def complete_fixture(self):
        return json.loads((Path(__file__).parent / 'fixtures/complete-results.json').read_text())

    def test_complete_scores_use_all_three_saved_summaries(self):
        report = self.complete_fixture()
        row = report['configurations'][0]
        row['passes'] = 999
        row['pass_rate'] = 999
        row['total'] = 1
        exported = project_complete(report)
        self.assertEqual(exported['configurations'][0]['passes'], 360)
        self.assertEqual(exported['configurations'][0]['total'], 600)
        self.assertEqual(exported['configurations'][0]['pass_rate'], .6)
        self.assertEqual(exported['configurations'][0]['personas'], row['personas'])
        self.assertEqual(row['passes'], 999)

    def test_partial_invalid_or_unattributed_results_do_not_replace_output(self):
        mutations = [
            lambda row: row['personas'].pop('riley'),
            lambda row: row['personas'].update(extra=row['personas']['alex']),
            lambda row: row['personas']['alex'].update(total=199),
            lambda row: row['personas']['alex'].update(total=201),
            lambda row: row['personas']['alex'].update(passes=True),
            lambda row: row['personas']['alex'].update(passes=-1),
            lambda row: row['personas']['alex'].update(passes=201),
            lambda row: row['personas']['alex'].update(pass_rate=.99),
            lambda row: row['evidence'].pop('recordings'),
            lambda row: row['evidence']['configuration'].update(url='javascript:alert(1)'),
            lambda row: row['evidence']['source'].update(sha256='wrong'),
            lambda row: row['personas']['morgan']['source'].update(url='https://secret@example.com/file'),
            lambda row: row.update(total_cost_usd_test_calls=-1),
            lambda row: row.update(total_cost_usd=-1),
            lambda row: row.update(total_cost_usd=float('nan')),
            lambda row: row.update(total_cost_usd=True),
            lambda row: row.pop('total_cost_scope'),
            lambda row: row.update(cost_note=''),
            lambda row: row.update(median_latency_seconds=float('nan')),
            lambda row: row.update(p95_latency_seconds=float('inf')),
            lambda row: row.update(p95_latency_seconds=1),
            lambda row: row.pop('agent_inference_cost_scope'),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            source, output = Path(temporary) / 'source.json', Path(temporary) / 'output.json'
            output.write_text('preserved')
            for index, mutate in enumerate(mutations):
                with self.subTest(index=index):
                    report = self.complete_fixture()
                    mutate(report['configurations'][0])
                    source.write_text(json.dumps(report))
                    with self.assertRaises((ValueError, KeyError)):
                        build_complete(source, output)
                    self.assertEqual(output.read_text(), 'preserved')

    def test_duplicate_configurations_and_ids_are_rejected(self):
        for same_id in (False, True):
            report = self.complete_fixture()
            duplicate = deepcopy(report['configurations'][0])
            if same_id:
                duplicate['model']['id'] = 'another-model'
            else:
                duplicate['id'] = 'another-id'
            report['configurations'].append(duplicate)
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                project_complete(report)

    def test_all_launch_combinations_and_unavailable_metrics_are_supported(self):
        report = self.complete_fixture()
        template = report['configurations'][0]
        report['configurations'] = []
        for harness, model in (('hermes', 'luna'), ('hermes', 'minimax'), ('claude-code', 'sonnet')):
            for memory in ('builtin', 'mem0', 'honcho', 'hindsight', 'supermemory'):
                row = deepcopy(template)
                row.update(id=f'{harness}-{model}-{memory}', total_cost_usd_test_calls=None,
                           median_latency_seconds=None, p95_latency_seconds=None)
                row['harness']['id'], row['model']['id'], row['memory']['id'] = harness, model, memory
                report['configurations'].append(row)
        self.assertEqual(project_complete(report), report)

    def test_build_check_rejects_stale_totals_and_accepts_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, output = Path(temporary) / 'source.json', Path(temporary) / 'output.json'
            report = self.complete_fixture()
            report['configurations'][0]['total'] = 200
            source.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, 'Overall scores'):
                build_complete(source, output, check=True)
            self.assertFalse(output.exists())
            build_complete(source, output)
            build_complete(output, output, check=True)

    def test_checked_in_complete_report_passes_build_validation(self):
        source = Path(__file__).resolve().parents[1] / 'content/official-results.json'
        build_complete(source, source, check=True)

    def test_cost_downloads_match_report_and_preserve_original_run_records(self):
        website = Path(__file__).resolve().parents[1]
        report = json.loads((website / 'content/official-results.json').read_text())
        original = json.loads((website / 'content/official-evidence.json').read_text())
        for row in report['configurations']:
            name = row['evidence']['configuration']['url'].rsplit('/', 1)[-1]
            archive = website / 'public/leaderboard/cost-records' / name
            self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(),
                             row['evidence']['configuration']['sha256'])
            with ZipFile(archive) as files:
                calculation = json.loads(files.read('cost-calculation.json'))
                self.assertEqual(calculation['configuration_id'], row['id'])
                if row['total_cost_usd'] is None:
                    self.assertIsNone(calculation['total_ingestion_and_test_cost_usd'])
                    self.assertEqual(calculation['test_calls']['total_cost_usd'],
                                     row['total_cost_usd_test_calls'])
                else:
                    if name in original:
                        self.assertEqual(calculation['original_configuration_sha256'], original[name]['sha256'])
                    else:
                        self.assertRegex(calculation['original_configuration_sha256'], r'^[0-9a-f]{64}$')
                        self.assertRegex(calculation['original_configuration_commit'], r'^[0-9a-f]{40}$')
                        records = set(files.namelist()) - {'cost-calculation.json', 'configuration-notes.md'}
                        self.assertEqual(set(calculation['run_record_sha256']), records)
                        for record in records:
                            self.assertEqual(hashlib.sha256(files.read(record)).hexdigest(),
                                             calculation['run_record_sha256'][record])
                    self.assertEqual(calculation['cost']['total_usd'], row['total_cost_usd'])
                    self.assertAlmostEqual(calculation['cost']['agent_usd'] + calculation['cost']['memory_usd'],
                                           row['total_cost_usd'])
                    self.assertEqual(set(calculation['results']), {'alex', 'morgan', 'riley'})
                    self.assertEqual(calculation['results'],
                                     {p: data['source'] for p, data in row['personas'].items()})
                self.assertTrue(files.read('configuration-notes.md'))
                for persona in ('alex', 'morgan', 'riley'):
                    if row['harness']['id'] == 'claude-code':
                        self.assertIn(f'{persona}/configuration.json', files.namelist())
                        self.assertIn(f'{persona}/ingestion-receipt.json', files.namelist())
                    else:
                        self.assertIn(f'{persona}/launch_manifest.json', files.namelist())

    def test_new_cost_totals_reconcile_with_their_calculations(self):
        root = Path(__file__).resolve().parents[1] / 'public/leaderboard/cost-records'
        def calculation(name):
            with ZipFile(root / f'{name}-configuration.zip') as files:
                return json.loads(files.read('cost-calculation.json'))

        for name in ('minimax-honcho', 'claude-honcho'):
            report = calculation(name)
            agent = report['agent_calculation']
            if name == 'minimax-honcho':
                amount = sum(persona[key] for persona in agent['personas'].values()
                             for key in ('ingestion_main_usd', 'ingestion_auxiliary_usd',
                                         'evaluation_main_usd', 'evaluation_auxiliary_usd'))
            else:
                amount = sum(persona[key] for persona in agent['personas'].values()
                             for key in ('ingestion_main_1h_usd', 'evaluation_usd', 'ingestion_helper_allowance_usd'))
            self.assertAlmostEqual(amount, report['cost']['agent_usd'])
            memory = sum(sum(persona['memory_components_usd'].values())
                         for persona in report['memory_calculation']['personas'].values())
            self.assertAlmostEqual(memory, report['cost']['memory_usd'])
            self.assertAlmostEqual(amount + memory, report['cost']['total_usd'])

        supermemory = calculation('luna-supermemory')
        self.assertAlmostEqual(supermemory['cost']['agent_usd'],
                               sum(p['agent_usd'] for p in supermemory['personas'].values()))
        self.assertAlmostEqual(supermemory['cost']['memory_usd'],
                               sum(p['memory_usd'] for p in supermemory['personas'].values()))
        memory = supermemory['memory_calculation']
        usage = memory['riley_recorded_usage']
        rates = memory['rates_usd_per_million_tokens']
        self.assertAlmostEqual(usage['cost_usd'],
                               usage['input_tokens'] * rates['input'] / 1_000_000
                               + usage['output_tokens'] * rates['output'] / 1_000_000)
        for persona in supermemory['personas'].values():
            self.assertAlmostEqual(persona['memory_usd'], persona['stored_document_tokens']
                                   * memory['memory_cost_per_stored_document_token_usd'])

        builtin = calculation('minimax-builtin')
        detail = builtin['calculation']
        amount = sum(p['main_usd'] + p['auxiliary_usd'] for p in detail['ingestion'].values())
        amount += detail['evaluation']['main_usd'] + sum(detail['evaluation']['auxiliary_usd_by_persona'].values())
        amount += detail['riley_prior_ingestion_attempts']['main_usd'] + detail['riley_prior_ingestion_attempts']['auxiliary_usd']
        self.assertAlmostEqual(amount, builtin['cost']['total_usd'])

        mem0 = calculation('minimax-mem0')
        detail = mem0['calculation']
        amount = sum(p['main_usd'] + p['auxiliary_usd'] for p in detail['ingestion'].values())
        amount += detail['evaluation']['main_usd'] + sum(detail['evaluation']['auxiliary_usd_by_persona'].values())
        amount += detail['missing_usage_estimate']['total_usd']
        self.assertAlmostEqual(amount, mem0['cost']['agent_usd'])
        backend = detail['backend_ingestion']
        memory = sum((p['input_tokens'] * backend['input_usd_per_million_tokens']
                      + p['output_tokens'] * backend['output_usd_per_million_tokens']) / 1_000_000
                     for p in backend['personas'].values())
        self.assertAlmostEqual(memory, mem0['cost']['memory_usd'])

        claude = calculation('claude-builtin')
        detail = claude['calculation']
        rates = claude['pricing']['claude-sonnet-5']
        amount = sum((p['input_tokens'] * rates['input_usd_per_million']
                      + p['output_tokens'] * rates['output_usd_per_million']
                      + p['cache_read_tokens'] * rates['cache_read_usd_per_million']
                      + p['cache_write_tokens'] * rates['cache_write_1h_usd_per_million']) / 1_000_000
                     for p in detail['ingestion'].values())
        amount += detail['evaluation_usd'] + detail['ingestion_helper_estimate']['total_usd']
        self.assertAlmostEqual(amount, claude['cost']['total_usd'])

    def test_preview_evidence_cannot_enter_published_report(self):
        report = self.complete_fixture()
        report['preview'] = True
        row = report['configurations'][0]
        row['evidence']['source']['url'] = '/leaderboard/evidence/source.json/'
        project_complete(report, local_preview=True)
        with self.assertRaisesRegex(ValueError, 'cannot be published'):
            project_complete(report)
        del report['preview']
        with self.assertRaisesRegex(ValueError, 'HTTPS'):
            project_complete(report)
        for url in ['/leaderboard/evidence/../private/', '//example.com/file',
                    '/leaderboard/evidence/file/?secret=x']:
            row['evidence']['source']['url'] = url
            with self.assertRaises(ValueError):
                project_complete(report, local_preview=True)

    def test_full_test_metrics_and_invalid_measurements(self):
        records = {}
        for persona, offset in [('alex', 0), ('morgan', 200), ('riley', 1000)]:
            records[persona] = {
                'persona': persona,
                'summary': {'total': 200, 'passes': 200, 'complete_200_test_run': True},
                'test_results': [{'test_id': str(i), 'passed': True, 'cost_usd': .01,
                                  'latency_seconds': offset + i} for i in range(200)],
            }
        metrics = summarize_tests(records)
        self.assertEqual(metrics['total_cost_usd_test_calls'], 6)
        self.assertEqual(metrics['median_latency_seconds'], 299.5)
        self.assertAlmostEqual(metrics['p95_latency_seconds'], 1169.05)
        row = records['alex']['test_results'][0]
        row['cost_usd'] = None
        self.assertIsNone(summarize_tests(records)['total_cost_usd_test_calls'])
        self.assertEqual(summarize_tests(records)['median_latency_seconds'], 299.5)
        row['latency_seconds'] = None
        self.assertIsNone(summarize_tests(records)['p95_latency_seconds'])
        for value in [-1, float('nan'), float('inf'), True, '1']:
            row['latency_seconds'] = value
            with self.assertRaisesRegex(ValueError, 'Invalid per-test'):
                summarize_tests(records)
        row['latency_seconds'] = 0
        row['passed'] = False
        with self.assertRaisesRegex(ValueError, 'pass count'):
            summarize_tests(records)
        row['passed'] = True
        row['test_id'] = '1'
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            summarize_tests(records)

    def fixture(self, directory: Path):
        summary = {
            provider: {"total": 200, "passes": passes, "pass_rate": passes / 200}
            for provider, passes in (("builtin", 82), ("mem0", 138), ("honcho", 126))
        }
        records = {"summary.json": summary, "paired_scores.json": {"saved": "verbatim"}}
        for provider in summary:
            records[f"{provider}.json"] = {
                "kind": "canonical_official_results", "persona": "morgan", "provider": provider,
                "model_id": "test-model", "summary": summary[provider],
                "test_results": [{"passed": False}],
            }
        hashes = {}
        for name, value in records.items():
            raw = json.dumps(value).encode()
            (directory / name).write_bytes(raw)
            hashes[name] = hashlib.sha256(raw).hexdigest()
        return records, hashes

    def test_saved_summaries_are_reported_without_reconstructing_scores(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            records, hashes = self.fixture(directory)
            with patch('build_results.SOURCE_HASHES', hashes):
                exported = project(directory)
            self.assertEqual(exported['summary'], records['summary.json'])
            self.assertEqual(exported['paired_scores'], records['paired_scores.json'])
            self.assertNotIn('test_results', exported)

    def test_changed_source_does_not_write_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _, hashes = self.fixture(directory)
            (directory / 'honcho.json').write_text('{}')
            output = directory / 'exported.json'
            with patch('build_results.SOURCE_HASHES', hashes), self.assertRaisesRegex(ValueError, 'honcho.json'):
                build(directory, output)
            self.assertFalse(output.exists())

    def test_inconsistent_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            records, hashes = self.fixture(directory)
            records['mem0.json']['model_id'] = 'different-model'
            raw = json.dumps(records['mem0.json']).encode()
            (directory / 'mem0.json').write_bytes(raw)
            hashes['mem0.json'] = hashlib.sha256(raw).hexdigest()
            with patch('build_results.SOURCE_HASHES', hashes), self.assertRaisesRegex(ValueError, 'same agent model'):
                project(directory)

    def test_checked_in_report_binds_the_approved_sources(self):
        report = json.loads((Path(__file__).resolve().parents[1] / 'content/morgan-results.json').read_text())
        self.assertEqual(report['source_sha256'], SOURCE_HASHES)
        for provider, passes in (('builtin', 82), ('mem0', 138), ('honcho', 126)):
            self.assertEqual(report['summary'][provider]['passes'], passes)
            self.assertEqual(report['summary'][provider]['total'], 200)


if __name__ == '__main__':
    unittest.main()
