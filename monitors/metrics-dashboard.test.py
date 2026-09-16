#!/usr/bin/env python3
"""Real-file boundary and publication checks; requires installed artifacts."""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import unittest
import uuid

spec = importlib.util.spec_from_file_location('dashboard', Path(__file__).with_name('metrics-dashboard.py'))
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent / 'testdata' / ('dashboard-' + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.ledger = self.root / 'ledger.jsonl'
        self.manifest = self.root / 'repos.conf'
        self.manifest.write_text('sample\tmain\tcheck\n')
        self.as_of = dashboard.timestamp('2026-01-08T00:00:00Z')

    def tearDown(self):
        shutil.rmtree(self.root)

    def write(self, rows):
        self.ledger.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')

    def event(self, kind, ts='2026-01-07T00:00:00Z', **fields):
        return dict(type=kind, ts=ts, repo='example/sample', pr=1, **fields)

    def summarize(self):
        repos, rows, source = dashboard.read_inputs(self.ledger, self.manifest, self.as_of)
        return dashboard.summarize(repos, rows, self.as_of, source)

    def detailed_review(self):
        contributor = dict(harness='sample-cli', family='sample-family',
            requested_model='requested-example', actual_model=None, exit_code=0,
            started_at='2026-01-06T23:59:00Z', finished_at='2026-01-07T00:00:00Z',
            duration_s=60, summary_present=True, completed=True, failure_reason=None,
            prompt_sha256='b' * 64)
        return self.event('review', post_rc=0, reviewers_ok=2, degraded=False,
            attempt_id='sample-attempt', head_sha='a' * 40,
            started_at=contributor['started_at'], finished_at=contributor['finished_at'],
            contributors=[contributor, copy.deepcopy(contributor)])

    def test_review_details_and_legacy_survive_publication(self):
        detailed = self.detailed_review()
        detailed['contributors'][1].update(completed=False, failure_reason='model_resolution')
        detailed.update(reviewers_ok=1, degraded=True)
        legacy = self.event('review', post_rc=0, reviewers_ok=2, degraded=False)
        self.write([detailed, legacy])
        data = self.summarize()
        self.assertEqual(data['recent_cases'][0]['contributors'], detailed['contributors'])
        self.assertEqual(data['recent_cases'][0]['head_sha'], 'a' * 40)
        for field in dashboard.REVIEW_DETAILS:
            self.assertIsNone(data['recent_cases'][1][field])
        args = argparse.Namespace(ledger=self.ledger, manifest=self.manifest, output=self.root / 'output', title='Sample evaluation', as_of=dashboard.iso(self.as_of))
        self.assertEqual(dashboard.publish(args), 0)
        snapshot = args.output / json.loads((args.output / 'current.json').read_text())['snapshot']
        evidence = json.loads((snapshot / 'evidence.json').read_text())
        self.assertEqual(evidence['events'][0]['contributors'], detailed['contributors'])
        page = (args.output / 'dashboard.html').read_text()
        for phrase in ['a' * 40, 'actual model: unknown', 'Duration: 60 seconds', 'model_resolution', 'Legacy event: head SHA', 'Cost is unmeasured']:
            self.assertIn(phrase, page)

    def test_malformed_review_details_fail_closed(self):
        mutations = [
            lambda r: r.pop('head_sha'),
            lambda r: r.update(head_sha='abc'),
            lambda r: r.update(attempt_id=''),
            lambda r: r.update(contributors=[]),
            lambda r: r.update(contributors=[None, None]),
            lambda r: r.update(finished_at='2026-01-07T00:00:01Z'),
            lambda r: r['contributors'][0].update(exit_code=True),
            lambda r: r['contributors'][0].update(actual_model=123),
            lambda r: r['contributors'][0].pop('requested_model'),
            lambda r: r['contributors'][0].update(prompt_sha256='x' * 64),
            lambda r: r['contributors'][0].update(duration_s=float('nan')),
            lambda r: r['contributors'][0].update(duration_s=10),
            lambda r: r['contributors'][0].update(started_at='2026-01-06T23:58:00Z'),
            lambda r: r['contributors'][0].update(finished_at='2026-01-06T23:59:00'),
            lambda r: r['contributors'][0].update(summary_present=False),
            lambda r: r['contributors'][0].update(completed='true'),
            lambda r: r['contributors'][0].update(failure_reason='model_resolution'),
            lambda r: r['contributors'][0].update(completed=False),
            lambda r: r['contributors'][0].update(completed=False, failure_reason='model_resolution'),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                row = self.detailed_review()
                mutate(row)
                self.write([row])
                with self.assertRaisesRegex(ValueError, 'ledger line 1:'):
                    self.summarize()

    def test_reviewer_text_cannot_inject_html(self):
        row = self.detailed_review()
        row['contributors'][0]['actual_model'] = '<script>alert(1)</script>'
        self.write([row])
        source = dashboard.markdown(self.summarize(), 'Sample')
        self.assertNotIn('<script>', source)
        self.assertIn('&lt;script&gt;', source)

    def test_boundaries_dry_runs_scope_and_observed_gaps(self):
        self.write([
            self.event('review', post_rc=0, reviewers_ok=2, degraded=False),
            self.event('review', '2026-01-08T00:00:00Z', post_rc=0, reviewers_ok=1, degraded=True),
            self.event('review', '2026-01-06T23:59:59Z', post_rc=1, reviewers_ok=0, degraded=True),
            self.event('merge', '2026-01-01T00:00:00Z'),
            self.event('merge', dry_run=True),
            self.event('heal', outcome='SCOPE_VIOLATION', duration_s=300),
            self.event('heal', outcome='PUSHED', duration_s=60),
            self.event('heal', outcome='RENDER_FAIL', duration_s=90),
            self.event('heal', outcome='SKIPPED', duration_s=80),
            self.event('sweep', result='ok', err_repos=0),
            self.event('sweep', '2026-01-07T00:10:00Z', result='preflight_failed'),
        ])
        data = self.summarize()
        day = data['windows']['24h']
        repo = day['repositories']['sample']
        self.assertEqual((repo['review_attempts'], repo['review_posts'], repo['full_review_posts'], repo['degraded_review_posts']), (2, 2, 1, 1))
        self.assertEqual((repo['heals'], repo['heal_duration_seconds'], repo['reported_pushes'], repo['scope_violations_excluded']), (1, 60, 1, 1))
        self.assertEqual(repo['undispatched_heals_excluded'], 2)
        self.assertEqual(day['healthy_observed_sweeps'], 1)
        self.assertEqual(day['largest_observed_sweep_gap_seconds'], 600)
        self.assertEqual(repo['recorded_merges'], 0)
        self.assertEqual(data['windows']['7d']['repositories']['sample']['recorded_merges'], 1)
        self.assertEqual(data['source']['dry_run_events_excluded'], 1)

    def test_future_malformed_and_ambiguous_org_fail(self):
        for rows in [
            [self.event('merge', '2026-01-08T00:00:01Z')],
            [self.event('merge', None)],
            [dict(type='merge', ts='2026-01-07T00:00:00Z', repo=None, pr=1)],
            [self.event('review', post_rc=0, reviewers_ok=2, degraded='false')],
            [self.event('merge'), dict(type='merge', ts='2026-01-07T00:00:00Z', repo='another/sample', pr=2)],
        ]:
            with self.subTest(rows=rows):
                self.write(rows)
                with self.assertRaises(ValueError):
                    self.summarize()

    def test_malformed_refresh_retains_evidence_and_renders_failure(self):
        self.write([self.event('merge')])
        args = argparse.Namespace(ledger=self.ledger, manifest=self.manifest, output=self.root / 'output', title='Sample evaluation', as_of=dashboard.iso(self.as_of))
        self.assertEqual(dashboard.publish(args), 0)
        pointer = (args.output / 'current.json').read_bytes()
        snapshot = args.output / json.loads(pointer)['snapshot']
        evidence = (snapshot / 'evidence.json').read_bytes()
        original_html = (snapshot / 'dashboard.html').read_bytes()
        self.ledger.write_text('{broken\n')
        self.assertEqual(dashboard.publish(args), 1)
        self.assertEqual((args.output / 'current.json').read_bytes(), pointer)
        self.assertEqual((snapshot / 'evidence.json').read_bytes(), evidence)
        self.assertEqual((snapshot / 'dashboard.html').read_bytes(), original_html)
        self.assertIn('Refresh failed at', (args.output / 'dashboard.html').read_text())
        self.assertFalse(json.loads((args.output / 'status.json').read_text())['ok'])
        self.assertFalse((args.output / '.refresh-lock').exists())

    def test_blind_sweep_is_observed_unhealthy(self):
        self.write([self.event('sweep', result='blind')])
        day = self.summarize()['windows']['24h']
        self.assertEqual(day['observed_sweeps'], 1)
        self.assertEqual(day['healthy_observed_sweeps'], 0)

    def test_stale_and_no_samples_are_explicit(self):
        self.write([self.event('merge', '2026-01-01T00:00:00Z')])
        data = self.summarize()
        self.assertEqual(data['freshness'], 'stale')
        self.assertIsNone(data['windows']['24h']['largest_observed_sweep_gap_seconds'])
        self.write([])
        self.assertEqual(self.summarize()['freshness'], 'no events')


if __name__ == '__main__':
    unittest.main()
