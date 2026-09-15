#!/usr/bin/env python3
"""Real-file boundary and publication checks; requires installed artifacts."""
import argparse
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
