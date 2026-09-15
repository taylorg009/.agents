#!/usr/bin/env python3
"""Render read-only monitor ledger evidence with the installed artifacts CLI."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
import math
from pathlib import Path
import re
import subprocess
import socket
import sys
import uuid


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('timestamp must be a string')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamps must include a timezone')
    return parsed.astimezone(timezone.utc)


def iso(value):
    return value.isoformat().replace('+00:00', 'Z')


def number(row, key, default=None):
    value = row.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('invalid numeric field: ' + key)
    return value


def read_inputs(ledger, manifest, as_of):
    manifest_bytes = manifest.read_bytes()
    repos = []
    for line in manifest_bytes.decode().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        fields = line.split()
        repo = fields[0]
        if len(fields) < 3 or not re.fullmatch(r'[\w.-]+(?:/[\w.-]+)?', repo) or repo in repos:
            raise ValueError('invalid or duplicate manifest repository')
        repos.append(repo)
    if not repos:
        raise ValueError('manifest has no repositories')
    raw = ledger.read_bytes()
    rows = []
    ignored = 0
    identities = {}
    for line_no, line in enumerate(raw.decode().splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError('event must be an object')
            time = timestamp(row['ts'])
            if time > as_of:
                raise ValueError('future event timestamp')
            dry = row.get('dry_run', False)
            if dry not in (False, True, 0, 1):
                raise ValueError('invalid dry_run')
            kind = row['type']
            if kind not in ('sweep', 'review', 'heal', 'merge'):
                raise ValueError('unsupported event type')
            if dry:
                ignored += 1
                continue
            if kind != 'sweep':
                full_repo = row['repo']
                if not isinstance(full_repo, str) or not re.fullmatch(r'[\w.-]+/[\w.-]+', full_repo):
                    raise ValueError('invalid repository slug')
                if not isinstance(row.get('pr'), int) or isinstance(row['pr'], bool) or row['pr'] <= 0:
                    raise ValueError('invalid pull request number')
                matches = [r for r in repos if r == full_repo or ('/' not in r and full_repo.endswith('/' + r))]
                if len(matches) != 1:
                    raise ValueError('repository absent or ambiguous in manifest')
                row['repository'] = matches[0]
                prior = identities.setdefault(matches[0], full_repo)
                if prior != full_repo:
                    raise ValueError('ambiguous organization for short repository name')
            if kind == 'review':
                number(row, 'post_rc')
                number(row, 'reviewers_ok')
                if type(row['reviewers_ok']) is not int or row['reviewers_ok'] not in (0, 1, 2):
                    raise ValueError('reviewers_ok must be 0, 1, or 2')
                if not isinstance(row.get('degraded'), bool):
                    raise ValueError('review degraded must be boolean')
                if row['degraded'] != (row['reviewers_ok'] != 2):
                    raise ValueError('review degraded inconsistent with successful reviewers')
            elif kind == 'heal':
                if row.get('outcome') not in ('PUSHED', 'DIAGNOSED', 'INFRA_FAIL', 'ABSTAINED', 'UNKNOWN', 'TIMEOUT', 'SCOPE_VIOLATION', 'RENDER_FAIL', 'SKIPPED'):
                    raise ValueError('unsupported heal outcome')
                number(row, 'duration_s')
            elif kind == 'sweep':
                if row.get('result') not in ('ok', 'preflight_failed', 'blind'):
                    raise ValueError('unsupported sweep result')
                if row['result'] == 'ok':
                    number(row, 'err_repos')
            row['time'] = time
            row['line'] = line_no
            rows.append(row)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f'ledger line {line_no}: {exc}') from exc
    return repos, rows, {'ledger_sha256': hashlib.sha256(raw).hexdigest(), 'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(), 'dry_run_events_excluded': ignored}


def summarize(repos, rows, as_of, source):
    last = max((r['time'] for r in rows), default=None)
    age = (as_of - last).total_seconds() if last else None
    result = {'as_of': iso(as_of), 'last_event_at': iso(last) if last else None, 'last_event_age_seconds': age,
              'recent_cases': [{k: r[k] for k in ('ts', 'type', 'repo', 'pr', 'post_rc', 'degraded', 'reviewers_ok', 'outcome') if k in r} for r in sorted(rows, key=lambda r: r['time'], reverse=True) if r['type'] in ('review', 'heal')][:20],
              'freshness': 'no events' if age is None else 'stale' if age > 1800 else 'recent', 'source': source, 'windows': {}}
    for name, days in [('24h', 1), ('7d', 7)]:
        start = as_of - timedelta(days=days)
        selected = [r for r in rows if start <= r['time'] <= as_of]
        sweeps = sorted((r for r in selected if r['type'] == 'sweep'), key=lambda r: r['time'])
        gaps = [(b['time'] - a['time']).total_seconds() for a, b in zip(sweeps, sweeps[1:])]
        per_repo = {}
        for repo in repos:
            events = [r for r in selected if r.get('repository') == repo]
            reviews = [r for r in events if r['type'] == 'review']
            heals = [r for r in events if r['type'] == 'heal' and r['outcome'] not in ('SCOPE_VIOLATION', 'RENDER_FAIL', 'SKIPPED')]
            per_repo[repo] = {'review_attempts': len(reviews), 'review_posts': sum(r['post_rc'] == 0 for r in reviews),
                'degraded_review_attempts': sum(r['degraded'] for r in reviews),
                'full_review_posts': sum(r['post_rc'] == 0 and not r['degraded'] and r['reviewers_ok'] == 2 for r in reviews),
                'degraded_review_posts': sum(r['post_rc'] == 0 and (r['degraded'] or r['reviewers_ok'] < 2) for r in reviews),
                'heals': len(heals), 'heal_duration_seconds': sum(r['duration_s'] for r in heals),
                'reported_pushes': sum(r['outcome'] == 'PUSHED' for r in heals),
                'recorded_merges': sum(r['type'] == 'merge' for r in events),
                'undispatched_heals_excluded': sum(r['type'] == 'heal' and r['outcome'] in ('RENDER_FAIL', 'SKIPPED') for r in events),
                'scope_violations_excluded': sum(r['type'] == 'heal' and r['outcome'] == 'SCOPE_VIOLATION' for r in events)}
        result['windows'][name] = {'start': iso(start), 'end': iso(as_of), 'observed_sweeps': len(sweeps),
            'healthy_observed_sweeps': sum(r['result'] == 'ok' and r['err_repos'] == 0 for r in sweeps),
            'largest_observed_sweep_gap_seconds': max(gaps) if gaps else None, 'repositories': per_repo,
            'source_lines': [r['line'] for r in selected]}
    return result


def duration(seconds):
    return 'unavailable' if seconds is None else f'{round(seconds / 60)} minutes'


def human_time(value):
    return timestamp(value).strftime('%d %b %Y, %H:%M UTC') if value else 'none'


def markdown(data, title):
    text = ['---', 'kind: visual', 'title: ' + json.dumps(title), 'summary: Recorded monitor activity and evidence limits', 'status: Operational snapshot', 'host: ' + json.dumps(socket.gethostname()), 'human: Monitor collector', '---', '', '## Story', '',
        f"Evidence as of **{human_time(data['as_of'])}**. Last event: **{human_time(data['last_event_at'])}**, age **{duration(data['last_event_age_seconds'])}**.", '',
        f'<div class="artifact-callout artifact-callout-warn">Freshness: {data["freshness"]}. More than 30 minutes without an event is stale. This page is a snapshot; age is measured at its evidence timestamp.</div>', '',
        'Healthy observed sweeps measure recorded samples, never uptime. Pushes are reported by agents, not verified fixes. Merges are ledger records, not independent GitHub verification. Scope violations and undispatched RENDER_FAIL/SKIPPED records are excluded from heal counts and durations. Dry runs are excluded. Windows include both boundary timestamps.', '']
    window = data['windows']['24h']
    week = data['windows']['7d']['repositories'].values()
    full = sum(r['full_review_posts'] for r in week)
    attempts = sum(r['review_attempts'] for r in week)
    text.extend(['<section class="artifact-grid artifact-grid-3">',
        f'<article class="artifact-stat"><div class="artifact-stat-value">{full} / {attempts}</div><div class="artifact-stat-label">7-day full-review posts / attempts</div></article>',
        f'<article class="artifact-stat"><div class="artifact-stat-value">{window["healthy_observed_sweeps"]} / {window["observed_sweeps"]}</div><div class="artifact-stat-label">24-hour healthy observed sweeps</div></article>',
        '<article class="artifact-stat"><div class="artifact-stat-value">Unmeasured</div><div class="artifact-stat-label">Quality: no adjudicated precision or recall</div></article>', '</section>', '', '## Figure', ''])
    healthy = window['healthy_observed_sweeps']
    total = window['observed_sweeps']
    width = 600 * healthy / total if total else 0
    text.extend(['<figure class="artifact-figure artifact-figure-diagram">',
        f'<svg viewBox="0 0 680 120" role="img" aria-label="Last 24 hours: {healthy} healthy out of {total} observed sweeps">',
        f'<text x="40" y="28" fill="#c8c8c8" font-size="16">Last 24 hours: {healthy} / {total} healthy observed sweeps</text>',
        '<rect x="40" y="46" width="600" height="28" fill="#374151"/>',
        f'<rect x="40" y="46" width="{width:.2f}" height="28" fill="#38bdf8"/>',
        '<text x="40" y="103" fill="#c8c8c8" font-size="13">Blue: healthy samples. Gray: other samples. No uptime inference.</text>', '</svg>', '<figcaption>Source: local ledger; evidence.json records hashes and source line numbers.</figcaption>', '</figure>', '', '## Data', ''])
    for name, window in data['windows'].items():
        text.extend([f'### Last {"24 hours" if name == "24h" else "7 days"}', '',
            f"{human_time(window['start'])} through {human_time(window['end'])}. Healthy observed sweeps: **{window['healthy_observed_sweeps']} / {window['observed_sweeps']}**. Largest gap between observed sweeps: **{duration(window['largest_observed_sweep_gap_seconds'])}**. No gap is inferred before the first sample or after the last.", '',
            '| Repository | Review attempts | Posts | Full posts | Degraded posts | Heals | Reported pushes | Recorded merges | Heal minutes | Scope excluded | Undispatched excluded |',
            '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|'])
        for repo, r in window['repositories'].items():
            values = [r[k] for k in ('review_attempts', 'review_posts', 'full_review_posts', 'degraded_review_posts', 'heals', 'reported_pushes', 'recorded_merges')]
            text.append('| ' + html.escape(repo) + ' | ' + ' | '.join(map(str, values)) + f" | {r['heal_duration_seconds']/60:.1f} | {r['scope_violations_excluded']} | {r['undispatched_heals_excluded']} |")
        text.append('')
    text.extend(['### Recent review and heal cases', '', 'Latest 20 recorded cases across the supplied ledger. Legacy rows do not identify the reviewed head SHA; these are not coverage claims.', '', '| Time | Pull request | Event | Recorded result |', '|---|---|---|---|'])
    for case in data['recent_cases']:
        outcome = case.get('outcome')
        if case['type'] == 'review':
            outcome = 'post failed' if case['post_rc'] else 'degraded post' if case['degraded'] or case['reviewers_ok'] < 2 else 'full post'
        text.append(f"| {human_time(case['ts'])} | [{case['repo']}#{case['pr']}](https://github.com/{case['repo']}/pull/{case['pr']}) | {case['type']} | {outcome} |")
    text.append('')
    text.extend(['Full posts require exactly two successful reviewers and no degraded flag. Counts describe events, not unique pull requests. Zero means no matching records in the supplied ledger, not proof of no activity.', '', '### Evidence', '',
        f"Ledger SHA-256: `{data['source']['ledger_sha256']}`", '', f"Manifest SHA-256: `{data['source']['manifest_sha256']}`", '',
        f"Dry-run events excluded: {data['source']['dry_run_events_excluded']}. Snapshot evidence: [JSON](evidence.json).", ''])
    return '\n'.join(text)


def atomic_write(path, content):
    staging = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    try:
        staging.write_bytes(content)
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)


def render(directory):
    process = subprocess.run(['artifacts', 'render', str(directory / 'dashboard.md'), '--no-link-previews'], cwd=directory, capture_output=True, text=True, timeout=120)
    if process.returncode != 0 or not (directory / 'dashboard.html').is_file():
        raise ValueError('artifacts render failed; previous evidence retained')


def publish(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    lock = output / '.refresh-lock'
    try:
        lock.mkdir()
    except FileExistsError:
        print('refresh lock exists; another refresh may be running; no snapshot changed', file=sys.stderr)
        return 1
    now = datetime.now(timezone.utc)
    run = output / 'snapshots' / (now.strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex)
    try:
        as_of = timestamp(args.as_of) if args.as_of else now
        if as_of > now:
            raise ValueError('as-of cannot be in the future')
        repos, rows, source = read_inputs(args.ledger, args.manifest, as_of)
        data = summarize(repos, rows, as_of, source)
        run.mkdir(parents=True)
        data['events'] = [{k: (iso(v) if isinstance(v, datetime) else v) for k, v in row.items() if k in ('ts', 'type', 'repo', 'repository', 'pr', 'post_rc', 'reviewers_ok', 'degraded', 'outcome', 'duration_s', 'result', 'err_repos', 'line')} for row in rows]
        (run / 'evidence.json').write_text(json.dumps(data, indent=2) + '\n')
        # The stable page points directly at this immutable evidence, never another run.
        source_md = markdown(data, args.title).replace('(evidence.json)', '(' + (run / 'evidence.json').as_uri() + ')')
        (run / 'dashboard.md').write_text(source_md)
        render(run)
        atomic_write(output / 'dashboard.html', (run / 'dashboard.html').read_bytes())
        atomic_write(output / 'current.json', json.dumps({'snapshot': str(run.relative_to(output)), 'as_of': data['as_of']}).encode())
        atomic_write(output / 'status.json', json.dumps({'ok': True, 'attempted_at': iso(now), 'as_of': data['as_of']}).encode())
        print(json.dumps({'ok': True, 'page': str(output / 'dashboard.html'), 'evidence': str(run / 'evidence.json')}))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        error = str(exc)
        status = {'ok': False, 'attempted_at': iso(now), 'error': error}
        try:
            current = json.loads((output / 'current.json').read_text())
            previous = output / current['snapshot']
            retained = (previous / 'dashboard.md').read_text()
            banner = f'<div class="artifact-callout artifact-callout-danger">Refresh failed at {human_time(iso(now))}. Retained evidence as of {human_time(current["as_of"])}. Error: {html.escape(error)}</div>\n\n'
            run = output / 'snapshots' / ('failure-' + uuid.uuid4().hex)
            run.mkdir(parents=True)
            (run / 'dashboard.md').write_text(retained.replace('## Story\n', '## Story\n\n' + banner, 1))
            render(run)
            atomic_write(output / 'dashboard.html', (run / 'dashboard.html').read_bytes())
            status['retained_as_of'] = current['as_of']
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            status['failure_banner_rendered'] = False
        atomic_write(output / 'status.json', json.dumps(status).encode())
        print(json.dumps(status), file=sys.stderr)
        return 1
    finally:
        lock.rmdir()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger', required=True, type=Path)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--title', default='Monitor evaluation')
    parser.add_argument('--as-of', help='Timezone-qualified ISO timestamp for reproducible evidence')
    return publish(parser.parse_args())


if __name__ == '__main__':
    sys.exit(main())
