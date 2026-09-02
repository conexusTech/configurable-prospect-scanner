#!/usr/bin/env node
/**
 * OKF conformance check — structural rules + process v3 rules.
 * Fails the build if the bundle is malformed. No external deps, no arguments.
 *
 *   node scripts/okf-check.mjs        # run from the REPO root
 *
 * Structural rules (pre-date v3):
 *   - okf/index.md exists and declares okf_version; nothing else declares it
 *   - okf/service.md exists with type/title/description/resource
 *   - every non-reserved concept has frontmatter with a non-empty `type`
 *   - no broken same-repo or repo-file links
 *
 * Process v3 rules (.claude/rules/methodology.md, and
 * aeo-workspace:/okf/playbooks/workspace-okf-flow-and-setup.md section 1.8):
 *   - a `type: Capability` file with no `#### Scenario:` fails
 *   - every scenario carries a `**Checked by:**` line
 *   - every capability has okf/qa/<name>.md
 *   - every cited check id exists in that checklist          (forwards traceability)
 *   - every check's Requirement matches a declared scenario   (backwards traceability)
 *   - a checklist with no `### Check:` fails; duplicate check ids fail
 *   - every check has Requirement, Surface, Automated
 *   - every check has a non-empty **Do** and **Expect** block
 *   - no status glyph outside roadmap.md and log.md
 *   - frontmatter `status:` other than `stub` fails
 *   - `type: PRD` / `PRD Section` / `TRD` fails; okf/prds/ or okf/trd/ existing fails
 *   - a brief with no `## Narrative` or no `#### Scenario:` fails
 *
 * okf/_proposals/ is an inbox, not curated content: valid link *targets*, exempt from
 * every frontmatter and v3 rule, and never scanned as a link *source*. That exemption
 * is what lets retired PRDs be parked at okf/_proposals/prds/ as input-only.
 */
import fs from 'node:fs';
import path from 'node:path';

const ROOT = process.cwd();
const OKF = path.join(ROOT, 'okf');
const RESERVED = new Set(['index.md', 'log.md']); // reserved filenames: no `type` required
const GLYPH_EXEMPT = new Set(['roadmap.md', 'log.md']);
const STATUS_GLYPHS = ['☐', '◐', '☑', '✅', '⛔']; // empty, half, ticked, white-heavy tick, no-entry
const errors = [];

// Running this from the workspace root would scan the workspace bundle, whose roadmap
// and playbooks legitimately carry status glyphs. Say so rather than report nonsense.
if (
  fs.existsSync(path.join(ROOT, 'scripts', 'workspace-check.mjs')) &&
  fs.existsSync(path.join(ROOT, '.claude', 'rules', 'methodology.md'))
) {
  console.error(
    'FAIL: this looks like the workspace root, not a repo root.\n' +
      '      The per-repo checker must run from a repo root; the workspace bundle is\n' +
      '      never scanned by it. Run `node scripts/workspace-check.mjs` here instead.'
  );
  process.exit(1);
}

function walk(dir) {
  const out = [];
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, e.name);
    if (e.isDirectory()) out.push(...walk(full));
    else if (e.isFile() && e.name.endsWith('.md')) out.push(full);
  }
  return out;
}

function parseFrontmatter(text) {
  const lines = text.split(/\r?\n/);
  if (lines[0]?.trim() !== '---') return null;
  const end = lines.indexOf('---', 1);
  if (end === -1) return null;
  const fm = {};
  for (const line of lines.slice(1, end)) {
    const m = line.match(/^([A-Za-z0-9_]+):\s*(.*)$/);
    if (m) fm[m[1]] = m[2].trim();
  }
  return fm;
}

const strip = (s) => (s || '').replace(/^["']|["']$/g, '').trim();
const rel = (f) => path.relative(ROOT, f).split(path.sep).join('/');

/**
 * Split a document into sections introduced by `headingRe`. A section body runs to the
 * next heading of the same or a shallower level, so a scenario cannot absorb the one
 * after it and report that one's `**Checked by:**` as its own.
 */
function sections(text, headingRe, level) {
  const out = [];
  let cur = null;
  for (const line of text.split(/\r?\n/)) {
    const m = line.match(headingRe);
    if (m) {
      cur = { title: m[1].trim(), body: [] };
      out.push(cur);
      continue;
    }
    const h = line.match(/^(#{1,6})\s/);
    if (h && h[1].length <= level) cur = null; // same or shallower heading closes it
    else if (cur) cur.body.push(line);
  }
  return out.map((s) => ({ title: s.title, body: s.body.join('\n') }));
}

/** A `**Label**` (or `**Label:**`) block with at least one non-empty line under it. */
function hasBlock(body, label) {
  const lines = body.split(/\r?\n/);
  const at = lines.findIndex((l) => new RegExp('^\\*\\*' + label + ':?\\*\\*\\s*$').test(l.trim()));
  if (at === -1) return false;
  for (const l of lines.slice(at + 1)) {
    const t = l.trim();
    if (!t) continue;
    if (/^\*\*[A-Za-z]/.test(t) || /^#{1,6}\s/.test(t)) break; // next block or heading
    return true;
  }
  return false;
}

if (!fs.existsSync(OKF)) {
  console.error('FAIL: okf/ directory not found');
  process.exit(1);
}

const allFiles = walk(OKF);
const files = allFiles.filter((f) => !f.split(path.sep).includes('_proposals'));
const read = new Map(files.map((f) => [f, fs.readFileSync(f, 'utf8')]));
const fmOf = new Map(files.map((f) => [f, parseFrontmatter(read.get(f))]));

// -- Structural 1 -- okf/index.md exists and declares okf_version --------------------
const rootIndex = path.join(OKF, 'index.md');
if (!fs.existsSync(rootIndex)) {
  errors.push('okf/index.md missing');
} else {
  const fm = parseFrontmatter(fs.readFileSync(rootIndex, 'utf8'));
  if (!fm || strip(fm.okf_version) !== '0.1') {
    errors.push('okf/index.md must declare okf_version: "0.1"');
  }
}
for (const f of files) {
  if (f === rootIndex) continue;
  if (fmOf.get(f)?.okf_version) {
    errors.push(`${rel(f)}: okf_version is declared in okf/index.md only`);
  }
}

// -- Structural 2 -- okf/service.md ---------------------------------------------------
const svc = path.join(OKF, 'service.md');
if (!fs.existsSync(svc)) {
  errors.push('okf/service.md missing');
} else {
  const fm = parseFrontmatter(fs.readFileSync(svc, 'utf8')) || {};
  for (const f of ['type', 'title', 'description', 'resource']) {
    if (!fm[f]) errors.push(`okf/service.md missing required frontmatter: ${f}`);
  }
}

// -- Structural 3 -- frontmatter `type`; and the v3 status / retired-type rules -------
for (const file of files) {
  const fm = fmOf.get(file);
  const name = path.basename(file);
  if (!RESERVED.has(name)) {
    if (!fm) {
      errors.push(`${rel(file)}: missing YAML frontmatter`);
      continue;
    }
    if (!strip(fm.type)) errors.push(`${rel(file)}: frontmatter missing non-empty 'type'`);
  }
  if (!fm) continue;

  // One status surface: frontmatter `status:` other than `stub` asserts state.
  const status = strip(fm.status);
  if (status && status !== 'stub') {
    errors.push(
      `${rel(file)}: frontmatter 'status: ${status}' — status lives in the roadmap; only 'stub' is allowed here`
    );
  }

  // Retired layers cannot come back.
  const type = strip(fm.type);
  if (['PRD', 'PRD Section', 'TRD'].includes(type)) {
    errors.push(
      `${rel(file)}: retired type '${type}' — requirements live in a brief, a roadmap row, a capability and a QA checklist`
    );
  }
}

// -- v3 -- retired directories --------------------------------------------------------
for (const d of ['prds', 'trd']) {
  if (fs.existsSync(path.join(OKF, d))) {
    errors.push(
      `okf/${d}/ exists — the layer is retired; park the documents under okf/_proposals/ as input`
    );
  }
}

// -- v3 -- no status glyph outside roadmap.md and log.md ------------------------------
for (const file of files) {
  if (GLYPH_EXEMPT.has(path.basename(file))) continue;
  const text = read.get(file);
  const found = STATUS_GLYPHS.filter((g) => text.includes(g));
  if (found.length) {
    errors.push(`${rel(file)}: status glyph ${found.join(' ')} — the roadmap is the only status surface`);
  }
}

// -- v3 -- briefs ---------------------------------------------------------------------
for (const file of files) {
  if (strip(fmOf.get(file)?.type) !== 'Brief') continue;
  const text = read.get(file);
  if (!/^##\s+Narrative\s*$/m.test(text)) {
    errors.push(`${rel(file)}: brief has no '## Narrative' — the why is the load-bearing half`);
  }
  if (!/^####\s+Scenario:/m.test(text)) {
    errors.push(`${rel(file)}: brief has no '#### Scenario:' — scenarios are the acceptance criteria`);
  }
}

// -- v3 -- capabilities and QA checklists, in both traceability directions ------------
const capFiles = files.filter((f) => strip(fmOf.get(f)?.type) === 'Capability');
const qaFiles = files.filter((f) => strip(fmOf.get(f)?.type) === 'QA Checklist');

/** Parse a checklist into id -> body, reporting duplicates and empties. */
function parseChecklist(file) {
  const checks = new Map();
  const found = sections(read.get(file), /^###\s+Check:\s*(.+)$/, 3);
  if (!found.length) errors.push(`${rel(file)}: QA checklist has no '### Check:' entries`);
  for (const c of found) {
    if (checks.has(c.title)) {
      errors.push(`${rel(file)}: duplicate check id '${c.title}'`);
      continue;
    }
    checks.set(c.title, c.body);
  }
  return checks;
}

const checklists = new Map(); // capability name -> Map(id -> body)
for (const file of qaFiles) checklists.set(path.basename(file, '.md'), parseChecklist(file));

for (const file of capFiles) {
  const name = path.basename(file, '.md');
  const scenarios = sections(read.get(file), /^####\s+Scenario:\s*(.+)$/, 4);
  if (!scenarios.length) {
    errors.push(
      `${rel(file)}: capability has no '#### Scenario:' — a requirement that cannot fail is not a requirement`
    );
    continue;
  }

  if (!fs.existsSync(path.join(OKF, 'qa', `${name}.md`))) {
    errors.push(`${rel(file)}: no okf/qa/${name}.md — every requirement has a check`);
  }
  const checks = checklists.get(name) ?? new Map();

  const titles = new Set();
  for (const s of scenarios) {
    if (titles.has(s.title)) errors.push(`${rel(file)}: duplicate scenario title '${s.title}'`);
    titles.add(s.title);

    const m = s.body.match(/^\*\*Checked by:?\*\*\s*(.+)$/m);
    if (!m) {
      errors.push(`${rel(file)}: scenario '${s.title}' has no '**Checked by:**' line`);
      continue;
    }
    const ids = m[1]
      .split(',')
      .map((x) => x.replace(/[`*]/g, '').trim())
      .filter(Boolean);
    if (!ids.length) errors.push(`${rel(file)}: scenario '${s.title}' cites no check id`);
    // Forwards: every cited check exists.
    for (const id of ids) {
      if (!checks.has(id)) {
        errors.push(
          `${rel(file)}: scenario '${s.title}' cites check '${id}', absent from okf/qa/${name}.md`
        );
      }
    }
  }

  // Backwards: every check cites a requirement this capability declares.
  for (const [id, body] of checks) {
    const req = body.match(/^\*\*Requirement:?\*\*\s*(.+)$/m);
    if (req && !titles.has(req[1].trim())) {
      errors.push(
        `okf/qa/${name}.md: check '${id}' requires '${req[1].trim()}', which no scenario in ${rel(file)} declares`
      );
    }
  }
}

// Every check is attributed, and can fail.
for (const file of qaFiles) {
  const name = path.basename(file, '.md');
  if (!capFiles.some((f) => path.basename(f, '.md') === name)) {
    errors.push(
      `${rel(file)}: no okf/capabilities/${name}.md — a checklist with no capability has outlived its requirements`
    );
  }
  for (const [id, body] of checklists.get(name) ?? new Map()) {
    for (const field of ['Requirement', 'Surface', 'Automated']) {
      if (!new RegExp('^\\*\\*' + field + ':?\\*\\*\\s*\\S', 'm').test(body)) {
        errors.push(`${rel(file)}: check '${id}' missing '**${field}:**'`);
      }
    }
    if (!hasBlock(body, 'Do')) errors.push(`${rel(file)}: check '${id}' has no non-empty '**Do**' block`);
    if (!hasBlock(body, 'Expect')) {
      errors.push(
        `${rel(file)}: check '${id}' has no non-empty '**Expect**' block — a check with no expected condition cannot fail`
      );
    }
  }
}

// -- Structural 4 -- link integrity. Cross-repo (repo:/...) and external are soft -----
const targets = new Set(allFiles.map((f) => '/' + path.relative(OKF, f).split(path.sep).join('/')));
const linkRe = /\]\(([^)\s]+)\)/g;
for (const file of files) {
  const text = read.get(file);
  let m;
  while ((m = linkRe.exec(text))) {
    const href = m[1].split('#')[0].trim();
    if (!href.endsWith('.md')) continue;
    if (/^https?:\/\//.test(href)) continue; // external
    if (/^[a-z0-9-]+:\//i.test(href)) continue; // cross-repo forward ref
    if (href.startsWith('/')) {
      if (!targets.has(href)) errors.push(`${rel(file)}: broken same-repo link -> ${m[1]}`);
    } else {
      // Relative links, with or without a leading ./ — a bare `prds/index.md` is as
      // broken as `./prds/index.md` and was previously skipped. They may legitimately
      // escape the bundle to point at repo files, so resolve on the filesystem.
      const fsTarget = path.resolve(path.dirname(file), href);
      if (!fsTarget.startsWith(ROOT + path.sep) || !fs.existsSync(fsTarget)) {
        errors.push(`${rel(file)}: broken repo-file link -> ${m[1]}`);
      }
    }
  }
}

if (errors.length) {
  console.error(`\nOKF conformance FAILED (${errors.length} issue${errors.length > 1 ? 's' : ''}):`);
  for (const e of errors) console.error('  - ' + e);
  process.exit(1);
}
console.log(
  `OKF conformance OK — ${files.length} concept files, ${capFiles.length} capabilities, ${qaFiles.length} checklists, 0 issues.`
);
