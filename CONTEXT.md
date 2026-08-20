# Plastic Promise Domain Glossary

## Workflow Session

A client conversation that may contain several independent workflow runs. A
session groups history but does not itself identify one active workflow cursor.

## Workflow Instance

One durable execution of an official workflow route. It owns a unique flow
scope, one monotonic stage cursor, and immutable execution receipts. Completing
an instance never makes it the default cursor for a later task.

## Workflow Route Candidate

A non-authoritative route suggested from the current user text. Deterministic
parsing, semantic classification, or a future derived route index may produce a
candidate. A candidate becomes active only after the authority gate accepts it.

## Authority Gate

The deterministic boundary that decides whether a route candidate may start or
advance a workflow. Model-invoked skills may be selected automatically; a
user-invoked skill requires current user authority and cannot be authorized by
memory, vector similarity, or model confidence alone.

## Semantic Classification Job

Durable, project-scoped work created when passive-memory rules do not find a
stable user fact. The job may use a cloud JSON model asynchronously, but its
output remains a proposal candidate rather than canonical memory.

## Promotion Job

Durable evaluation work for an eligible pending proposal. Every terminal or
retrying outcome has a machine-readable reason. Reconciliation recreates
missing work without changing proposal eligibility or bypassing promotion
policy.

## Knowledge System Glossary

The knowledge context turns curated source material into versioned, cited, searchable knowledge while keeping memory, source evidence, and generated synthesis distinct.

### Language

**Knowledge Space**:
A project-owned graph of sources, knowledge domains, claims, and generated artifacts that share one access policy and embedding generation.
_Avoid_: Knowledge base instance, wiki folder

**Source**:
A logical external or uploaded origin whose content may change over time.
_Avoid_: Document, file

**Source Version**:
A Source-bound immutable snapshot whose bytes are identified by a content hash.
_Avoid_: Latest file, current document

**Raw Artifact**:
The preserved original bytes of a Source Version.
_Avoid_: Chunk, extracted text

**Normalized Document**:
The text-and-structure projection extracted from a Source Version without changing its meaning.
_Avoid_: Summary, wiki page

**Evidence Chunk**:
A deterministic, stable span of a Normalized Document used for exact citation and audit.
_Avoid_: Semantic chunk, passage

**Semantic Unit**:
A model-generated grouping or interpretation of one or more Evidence Chunks used for retrieval and synthesis.
_Avoid_: Evidence, source text

**Knowledge Domain**:
An automatically maintained classification view such as development, operations, or finance within a Knowledge Space.
_Avoid_: Behavior domain, project, access scope

**Claim**:
A bounded assertion extracted from evidence, with temporal scope, provenance, confidence, and conflict state.
_Avoid_: Fact, memory

**Citation**:
A reference from a Claim or Knowledge Artifact to one or more exact Evidence Chunks.
_Avoid_: Link, source name

**Knowledge Artifact**:
A versioned, model-maintained projection such as a source summary, entity page, concept page, topic page, overview, or saved synthesis.
_Avoid_: Source, canonical fact

**Promotion**:
The governed transition that makes a validated Knowledge Artifact or Claim eligible for ordinary retrieval.
_Avoid_: Save, publish

**Knowledge Generation**:
A complete derived search index bound to one embedding identity, chunking identity, and verified source revision set.
_Avoid_: Vector database, model version

**Source Candidate**:
A discovered or uploaded Source that remains quarantined until source policy admits it.
_Avoid_: Draft knowledge, pending memory

**Internal Knowledge Candidate**:
A proposed generalization derived from repeated memories or operational outcomes, marked as internal experience rather than external authority.
_Avoid_: Canonical knowledge, verified fact

**Security Finding**:
An immutable observation emitted by a versioned security scanner against an exact project revision and scan subject.
_Avoid_: Vulnerability truth, active rule

**Evolution Evidence**:
A project-scoped, provenance-preserving observation admitted for evaluating a possible behavior or policy change.
_Avoid_: Memory, knowledge claim, rule

**Independent Evidence**:
Evolution Evidence whose causal origin is not the same scanner run, source version, generated projection, or descendant feedback cycle as the evidence it corroborates.
_Avoid_: Repeated citation, usage count, self-confirmation

**Evolution Candidate**:
A proposed, versioned change to a review rule, workflow policy, skill route, or maintenance behavior that has not completed controlled validation.
_Avoid_: Recommendation, active rule

**Evolution Rule**:
A versioned behavior or policy projection that has passed governed validation and is eligible to influence live decisions within its declared scope.
_Avoid_: Prompt hint, finding, candidate

**Tombstone**:
A lifecycle marker that removes content from ordinary retrieval while preserving lineage until purge eligibility.
_Avoid_: Delete, archive

**Purge**:
The policy-governed physical removal of content after impact analysis, backup, authorization, and retention checks.
_Avoid_: Forget, garbage collection

## Deployment Contract Glossary

**Deployment Profile**:
A stable, named topology describing where the runtime, canonical state, derived index, workers, and inference responsibilities live.
_Avoid_: Mode, release edition, knowledge domain

**Deployment Module**:
A capability that may be included in a Deployment Profile, with a declared risk tier, supported profiles, and dependency set.
_Avoid_: Python package, plugin, service process

**Deployment Manifest**:
A versioned, secret-free declaration of one deployment identifier, one Deployment Profile, and explicit optional module selections.
_Avoid_: Environment file, credentials file, live configuration

**Resolved Deployment Plan**:
The deterministic result of validating a Deployment Manifest, including the profile baseline and dependency-closed module list; it is not an apply command.
_Avoid_: Deployment execution, production revision

**Migration Operation**:
A server-owned, ordered transition from one verified runtime topology to another, including rehearsal, cutover, rollback, and a durable safe receipt.
_Avoid_: Deploy apply, shell script, container switch

**Migration Operation Plan**:
A short-lived, secret-free plan that binds one Migration Operation to the current canonical-state, artifact, runtime, node, and derived-index evidence. It is distinct from a Deployment Center inspection hash and cannot itself authorize mutation.
_Avoid_: Preview plan, browser confirmation, deployment execution

**Execution Grant**:
An explicit, operation-bound authorization that matches a fresh Migration Operation Plan and its risk acknowledgements before a server-owned transition can begin.
_Avoid_: Plan hash, standing approval, UI click

**Canonical State Transition**:
The exclusive pp-core operation that backs up, verifies, migrates, restores, or otherwise changes canonical SQLite under a deployment lease.
_Avoid_: CLI database write, node migration, container-owned database

**Migration Receipt**:
A secret-free, immutable outcome record for a Migration Operation, including safe evidence hashes, ordered phase result, rollback state, and stable reason code.
_Avoid_: Service log, deployment plan, build receipt

**Local Heterogeneous Inference Node**:
A separately registered local host that offers bounded embedding and reranking contracts without canonical SQLite write access.
_Avoid_: GPU server, database replica, automatic LAN-discovered worker

## Release Builder Glossary

**Release Builder**:
A maintainer-operated, request-triggered local release module that builds Plastic Promise itself and may publish or deploy only the exact reviewed source named by a Release Request.
_Avoid_: General build farm, arbitrary-code runner, release daemon

**Release Request**:
An immutable, secret-free declaration of a version, an exact source commit, an allowed source channel, and explicit release actions.
_Avoid_: Release configuration, shell script, deployment plan

**Desktop Confirmation**:
A time-limited local acknowledgement bound to the complete Release Request hash before an interactive stable release can begin.
_Avoid_: Login, standing approval, remembered consent

**Release Receipt**:
A secret-free, immutable record of one Release Request phase, its inputs, observed evidence hashes, result, and terminal reason where applicable.
_Avoid_: Log file, release notes, memory

**Release Evidence Chain**:
The ordered set of protected validation, immutable artifact, deployment receipt, and release-repository evidence that binds one stable version to one exact source commit.
_Avoid_: Build log, CI status

**Builder Mode**:
The capability boundary of a Release Builder installation. `desktop-interactive` may use an existing desktop identity after confirmation; `headless-builder` is limited to local no-secret build and smoke work.
_Avoid_: Deployment profile, runtime mode

## Project Coordination Glossary

**Delivery Program Contract**:
The normative, revision-bound agreement for an arbitrary dependency graph of Delivery Units, their requirements, hard gates, evidence obligations, and generated governance projections.
_Avoid_: Six-PR framework, workflow plan, release checklist

The generic Delivery Program Contract is a reusable contract model. The
Union Six-PR JSON remains the sole canonical normative source for the current
historical six-unit program; a generic compiler, adapter, projection, or vector
index cannot silently become a second authority.

**Delivery Program**:
One governed delivery effort compiled from a Delivery Program Contract; its number and kinds of Delivery Units are defined by the instance rather than by the platform.
_Avoid_: Project Scope, workflow route, fixed PR chain

**Delivery Unit**:
A dependency-addressable body of delivery obligations that can be assessed independently and may be represented by a pull request, milestone, migration wave, release phase, or work package.
_Avoid_: Agent Work Item, Skill Atom, mandatory PR

**Governance Clause**:
A deterministic, digest-bound projection of one requirement, invariant, gate, or evidence obligation for retrieval and impact analysis; retrieval relevance never grants authority or satisfies the clause.
_Avoid_: Semantic Unit, canonical memory, vector match

**Union Six-PR Contract**:
The normative delivery agreement in which each of the six planned PRs owns both its deployment responsibility and its project-collaboration responsibility; neither side alone constitutes completion.
_Avoid_: Collaboration add-on, parallel collaboration roadmap, deployment-only roadmap

**Coordination Plane**:
The project-scoped, short-lived record of Agent presence, work leases, progress, findings, blockers, conflicts, and result receipts.
_Avoid_: Shared memory, Agent chat, canonical project state

**Project Working Set**:
A rebuildable, task-lifetime projection of the current goal, plan revision, active work, accepted results, blockers, and conflicts.
_Avoid_: Canonical memory, project database, workflow authority

**Canonical Memory Plane**:
The long-lived governed record of accepted facts, decisions, principles, and reusable experience; transient coordination activity never enters it directly.
_Avoid_: Activity stream, working set, pending proposal

**Project Scope**:
The canonical project boundary shared by Agent sessions, work, events, receipts, retrieval, and lifecycle decisions.
_Avoid_: Caller-provided project label, repository path, workspace name

**Coordination Session**:
A bounded collaboration lifetime within one Project Scope whose Agents, work, events, cursors, and receipts are interpreted together.
_Avoid_: Chat session, workflow route, global Agent pool

**Agent Identity**:
The stable, secret-free identity of one collaborating Agent, distinct from its temporary session, claimed role, model, or provider credentials.
_Avoid_: Display name, API identity, role assertion

**Agent Session**:
A project- and coordination-session-scoped presence identity whose current state and policy binding determine whether an Agent may participate in work.
_Avoid_: Agent name, model identity, workflow session

**Agent Policy Binding**:
A server-issued, revision-bound statement of the exact project role, audience, and allowed collaboration operations for one Agent Session.
_Avoid_: Caller role claim, trust score, model capability

**Agent Registry**:
The authoritative project view of Agent Sessions and their current lifecycle and policy bindings.
_Avoid_: Process list, model catalog, peer transcript

**Project Work Board**:
The authoritative project boundary for proposing, leasing, submitting, reviewing, accepting, returning, and reconciling Work Items.
_Avoid_: SQL handler, generic task queue, compute scheduler

**Work Item**:
A project-scoped unit of intended collaboration whose lifecycle can be leased, submitted, reviewed, accepted, or returned for rework.
_Avoid_: Prompt, task queue row, compute job

**Work Lease**:
A time-bounded, fenced claim that lets one eligible Agent attempt one Work Item without granting unrelated project authority.
_Avoid_: Assignment text, Work Receipt, standing permission

**Collaboration Event**:
An append-only, audience-scoped observation about project coordination, carrying bounded evidence and causal references rather than private reasoning.
_Avoid_: Memory, instruction, chat message

**Event Cursor**:
A server-issued monotonic position in one project and Coordination Session event log, used to resume incremental reads without treating transport delivery as correctness evidence.
_Avoid_: SSE event ID, timestamp, client offset

**Event Page**:
A bounded, digest-bound server projection of Collaboration Events between an input and output Event Cursor, including source revision and gap/replay semantics.
_Avoid_: Event list, peer transcript, push notification

**Work Receipt**:
An immutable record binding one Work Item attempt to its Project Scope, Agent Session, Work Lease fence, source revision, and declared work digest.
_Avoid_: Assignment, lease token, completion marker

**Result Receipt**:
An immutable, secret-free claim binding a submitted result to its Work Item, lease fence, source identity, evidence references, and content digest; it is not acceptance by itself.
_Avoid_: Completion flag, result body, acceptance verdict

**Review Receipt**:
An immutable reviewer decision claim bound to an independent reviewer Agent Session, policy revision, source boundary, evidence digest, and the exact Work and Result Receipts being reviewed; it is not Accepted Work by itself.
_Avoid_: Review comment, task verification, approval label

**Acceptance Receipt**:
A server-authenticated immutable decision that validates the submitter and reviewer sessions, policy, source boundary, evidence, conflict state, Work Receipt, Result Receipt, and Review Receipt together.
_Avoid_: Caller-signed receipt, accepted flag, trusted wrapper

**Accepted Work**:
A submitted result whose Work Item, Work Lease, evidence, reviewer authority, and acceptance receipt have been verified together.
_Avoid_: Completed output, artifact reference, Agent claim

**Awareness Projection**:
A role-aware, cursor-bounded view of relevant Coordination Plane changes that has no execution or canonical-memory authority.
_Avoid_: Context memory, full peer transcript, Agent command

**Agent Awareness Projection**:
The concrete rebuildable projection supplied to one authenticated Agent Session from a bounded Event Page, filtered by project, coordination session, role, audience, cursor, and redaction policy.
_Avoid_: Caller-built context, shared prompt, canonical state

**Collaboration Memory Promoter**:
The server-owned adapter that may translate Accepted Work into a pending memory proposal while preserving the complete Acceptance Receipt lineage. It cannot directly write Canonical Memory.
_Avoid_: Stop Hook capture, peer progress storage, automatic adoption

The authoritative promotion chain is:

```text
ReviewReceipt
  → server verifies reviewer/session/policy/source/evidence
  → AcceptanceReceipt
  → Accepted Work
  → pending memory proposal
  → governed adoption
  → Canonical Memory
```
