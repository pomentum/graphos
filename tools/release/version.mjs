#!/usr/bin/env node
/**
 * version.mjs — one version for the server, the client and the web UI.
 *
 *   node tools/release/version.mjs 0.4.0               set 0.4.0 everywhere, roll the changelog
 *   node tools/release/version.mjs 0.4.0 --date D      the same, dated D (YYYY-MM-DD; default today, UTC)
 *   node tools/release/version.mjs --check             exit 1 unless every version is the same
 *   node tools/release/version.mjs --check 0.4.0       ... and equal to 0.4.0 (a tag), with a changelog entry
 *
 * graphos-api, graphos-ui and graphos-client are released together under one version, which
 * lives in six places (both pyproject.toml files, both __version__ strings, the UI's package.json
 * and package-lock.json). Setting it also turns the changelog's "## Unreleased" heading into
 * "## <version> — <date>", dates the whitepaper and updates the README's release example. CI runs
 * --check on every push, and the release workflows run it against the tag they publish.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const SEMVER = /^\d+\.\d+\.\d+$/;
const DAY = /^\d{4}-\d{2}-\d{2}$/;

/** Where the version is written: a file and how to read and replace it there. */
const PLACES = [
  { file: 'services/graphos/pyproject.toml', ...line(/^version = "([^"]+)"$/m, (v) => `version = "${v}"`) },
  { file: 'clients/python/pyproject.toml', ...line(/^version = "([^"]+)"$/m, (v) => `version = "${v}"`) },
  { file: 'services/graphos/src/graphos/__init__.py', ...line(/^__version__ = "([^"]+)"$/m, (v) => `__version__ = "${v}"`) },
  { file: 'clients/python/src/graphos_client/__init__.py', ...line(/^__version__ = "([^"]+)"$/m, (v) => `__version__ = "${v}"`) },
  { file: 'services/graphos-ui/package.json', ...json((doc) => doc.version, (doc, v) => { doc.version = v; }) },
  {
    file: 'services/graphos-ui/package-lock.json',
    ...json(
      (doc) => (doc.version === doc.packages?.['']?.version ? doc.version : `${doc.version} / ${doc.packages?.['']?.version}`),
      (doc, v) => { doc.version = v; doc.packages[''].version = v; },
    ),
  },
];

function line(pattern, render) {
  return {
    read: (text) => text.match(pattern)?.[1],
    write: (text, v) => text.replace(pattern, render(v)),
  };
}

// npm writes package files as 2-space JSON with a final newline; so does this.
function json(get, set) {
  return {
    read: (text) => get(JSON.parse(text)),
    write: (text, v) => {
      const doc = JSON.parse(text);
      set(doc, v);
      return `${JSON.stringify(doc, null, 2)}\n`;
    },
  };
}

const read = (file) => readFileSync(join(ROOT, file), 'utf8');
const write = (file, text) => writeFileSync(join(ROOT, file), text);

function versions() {
  return PLACES.map((place) => ({ file: place.file, version: place.read(read(place.file)) }));
}

function check(tag) {
  const found = versions();
  const errors = [];
  for (const { file, version } of found) {
    if (!version) errors.push(`no version found in ${file}`);
  }
  const distinct = [...new Set(found.map((f) => f.version))];
  if (distinct.length > 1) {
    errors.push(`versions differ: ${found.map((f) => `${f.file}=${f.version}`).join(', ')}`);
  }
  if (tag !== undefined) {
    if (!SEMVER.test(tag)) errors.push(`tag ${tag} is not a version (X.Y.Z)`);
    for (const { file, version } of found) {
      if (version !== tag) errors.push(`tag ${tag} does not match version ${version} in ${file}`);
    }
    if (!new RegExp(`^## ${tag.replace(/\./g, '\\.')} — `, 'm').test(read('CHANGELOG.md'))) {
      errors.push(`CHANGELOG.md has no "## ${tag} — <date>" entry`);
    }
  }
  for (const error of errors) console.error(process.env.GITHUB_ACTIONS ? `::error::${error}` : `error: ${error}`);
  if (errors.length) process.exit(1);
  console.log(`version ${distinct[0]} in all ${found.length} places${tag ? `, matching tag ${tag}` : ''}`);
}

function set(version, date) {
  const changed = [];
  for (const place of PLACES) {
    const before = read(place.file);
    const after = place.write(before, version);
    if (after !== before) {
      write(place.file, after);
      changed.push(place.file);
    }
  }

  // The changelog: "## Unreleased" becomes this version, unless it already has an entry.
  const changelog = read('CHANGELOG.md');
  const heading = `## ${version} — `;
  if (!changelog.includes(heading)) {
    if (!/^## Unreleased$/m.test(changelog)) {
      console.error(`error: CHANGELOG.md has neither "## Unreleased" nor an entry for ${version}`);
      process.exit(1);
    }
    write('CHANGELOG.md', changelog.replace(/^## Unreleased$/m, `${heading}${date}`));
    changed.push('CHANGELOG.md');
  }

  // The whitepaper's date, and the README's release example (X.Y.Z and its X.Y image tag).
  const minor = version.split('.').slice(0, 2).join('.');
  const docs = [
    ['docs/whitepaper.html', /(Whitepaper · version )\d{4}-\d{2}-\d{2}/, `$1${date}`],
    ['README.md', /(Push a version tag such as `)\d+\.\d+\.\d+(`)/, `$1${version}$2`],
    ['README.md', /(graphos-api:)\d+\.\d+\.\d+(` and `:)\d+\.\d+(`)/, `$1${version}$2${minor}$3`],
  ];
  for (const [file, pattern, replacement] of docs) {
    const before = read(file);
    const after = before.replace(pattern, replacement);
    if (after !== before) {
      write(file, after);
      if (!changed.includes(file)) changed.push(file);
    }
  }

  console.log(changed.length ? `set ${version}:\n  ${changed.join('\n  ')}` : `already ${version}; nothing to change`);
  check(version);
}

const args = process.argv.slice(2);
if (args[0] === '--check') {
  check(args[1]);
} else if (args[0] && SEMVER.test(args[0])) {
  const at = args.indexOf('--date');
  const date = at >= 0 ? args[at + 1] : new Date().toISOString().slice(0, 10);
  if (!date || !DAY.test(date)) {
    console.error('error: --date takes YYYY-MM-DD');
    process.exit(2);
  }
  set(args[0], date);
} else {
  console.error('usage: node tools/release/version.mjs <X.Y.Z> [--date YYYY-MM-DD] | --check [X.Y.Z]');
  process.exit(2);
}
