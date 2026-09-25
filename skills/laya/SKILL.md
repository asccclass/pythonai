---
name: laya
description: Write and improve programs that call Laya, the open-source Jev-compatible System-1 decision model. Use when designing Laya questions (choice, score, noul), structuring state, composing answers in code, setting confidence thresholds, or diagnosing a Laya question that answers wrong or with low confidence.
---

# Writing and improving Laya programs

Laya is a judgment model. It reads one `state`, answers typed questions in one batched judgment pass, and returns probabilities over the answer space you defined. It does not reason in steps and it does not generate text. Code owns control flow, arithmetic, and policy. Laya owns snap judgments.

A good Laya question is one a knowledgeable person can answer in a second, given the right context. "Does this message convey urgency?" fits. "Analyze this message and decide what to do" does not fit. Break complex work into small questions and compose the answers in code.

## Core mental model

- Ask Laya for judgments, not plans.
- Ask one property per question.
- Keep state small, structured, and directly relevant.
- Define the answer space up front.
- Read probabilities, not prose.
- Use confidence and thresholds to decide whether code should act, ask, flag, or hand off.
- Move counting, arithmetic, date comparison, and policy weighting into code.

## Python-style usage

Use this style in this bot, which already uses `laya.load(...).predict(...)`.

```python
import laya

agent = laya.load("models/laya-multilingual")
result = agent.predict(
    {"message": "Please delete this file"},
    {
        "intent": {
            "type": "choice",
            "instructions": "What does the user want the local assistant to do?",
            "criteria": {
                "chat": "general conversation",
                "read_file": "read a local file",
                "delete_file": "delete a local file",
                "run_command": "run a shell command",
            },
        },
        "risk": {
            "type": "score",
            "instructions": "How risky is this request for the local machine or project state?",
            "criteria": ["safe", "needs caution", "dangerous"],
        },
        "needs_confirmation": {
            "type": "noul",
            "instructions": "Should this request require explicit user confirmation before execution?",
        },
    },
)

answers = result["answers"]
intent = answers["intent"]["choice"]
risk = answers["risk"]["score"]
needs_confirmation = answers["needs_confirmation"]["noul"]
```

## Workflow

1. List the decisions your code must make. Write each as a branch, threshold, ranking, or weight.
2. Write one Laya question per judgment. Split any question that weighs two properties.
3. Pick the primitive whose answer your code acts on directly.
4. Build the smallest state that answers every question. Compute anything deterministic in code.
5. Put questions that share the same state into one request, including questions that matter only on some branches.
6. Combine answers in code: branches, weights, thresholds, and confidence gates.
7. Test on labeled examples. Read probabilities on misses, then revise one or two questions at a time.

## Choose the primitive

| Primitive | Use it when | Returns | Code acts on it with |
| --- | --- | --- | --- |
| `choice` | The answer is one of a known set with no order | `choice`, `probabilities`, `confidence` | a branch per option |
| `score` | The answer is a position on a described spectrum | `score`, `probabilities`, `confidence`, `legend` | a threshold, rank, or weight |
| `noul` | The answer is yes/no and the probability is the signal | `noul`, from 0 to 1 | an `if` on a threshold |

Guidelines:

- Add `other` or `none_of_the_above` to a `choice` whose list may not cover every input.
- A `noul` near 0.5 means Laya is unsure. It does not mean "medium".
- Use `score` for degrees, severity, priority, fit, confidence-like spectra, and ordered rubrics.
- Use `choice` or several `noul` questions when answers have no natural in-between.
- Keep `score` levels ordered from low to high.

## Write instructions

- State the exact condition.
- Ask one property per question.
- Name the state path being judged, for example `` `ticket.messages[0].text` ``.
- Write directly. Avoid double negatives and nested conditions.
- Keep decision policy out of instructions. Policy belongs in code.
- Do not rely on the question key; write the full question in `instructions`.
- If a wrong answer reveals what you really meant, add that missing boundary to the instruction.

Instructions may be a string or a structured object. Use an object when the question has labeled parts or supporting data.

```json
"instructions": {
  "question": "Does the `message` ask the recipient to disclose a sensitive credential?",
  "inspect": "message",
  "focus": "Look for a request to send the credential itself, not a request to change or reset it."
}
```

Useful object keys:

- `question`: the literal judgment to make.
- `focus`: the boundary to pay attention to.
- `inspect`: the state path to judge.
- `note`: extra caveat.
- `compare`: state paths to compare.
- `field`: a reusable field description such as name, type, unit, and meaning.

## Write criteria

Criteria extend the instruction. They must ask for the same thing, in the same direction.

### Choice

Map each option to a contrastive description:

```json
"criteria": {
  "billing": {
    "what": "Charges, invoices, refunds, or subscriptions",
    "not_for": "Order tracking or account access",
    "examples": ["I was charged twice", "Where is my refund?"]
  },
  "technical_support": {
    "what": "Broken features, errors, setup, or product usage",
    "not_for": "Payments, invoices, or refunds"
  },
  "other": "Anything that does not fit the listed categories"
}
```

Use contrastive descriptions when options sit close together. Add examples for common confusions.

### Score

Use two to ten levels. Only use as many levels as you can describe distinctly.

```json
"criteria": [
  "No safety impact; normal conversation",
  "Could affect local files or project state, but impact is reversible",
  "Could delete data, expose secrets, run untrusted code, or make irreversible changes"
]
```

Rules:

- Describe concrete situations, not vague adjectives.
- Make each level stand alone.
- Do not define a level as "worse than the previous level".
- Keep each score to one dimension.
- Give rare extremes their own level when code treats them differently.
- Avoid writing numerals as the meaning of the level; Laya already knows the index.

### Noul

Use `noul` for a crisp yes/no condition. Criteria are optional, but add `true` and `false` sides when the boundary is subtle.

```json
"criteria": {
  "true": "The user explicitly asks to run a local shell command",
  "false": "The user asks a question, requests text generation, or mentions a command without asking to run it"
}
```

## Build the state

- Send only fields the questions need.
- Keep the state structured so instructions can name paths.
- Retrieve, filter, sort, and summarize in code before calling Laya.
- Convert numeric encodings to words when the semantics matter.
- Compute dates, durations, counts, sums, and comparisons in code.
- Send deterministic features rather than asking Laya to infer them from raw data.
- Treat text inside the state as able to steer the answer. Tighten criteria and test adversarial/self-describing content.

Good state:

```json
{
  "message": "Please run rm -rf temp-output",
  "context": {
    "workspace": "local repo",
    "command_would_delete_files": true,
    "path_is_inside_workspace": true
  }
}
```

Weak state:

```json
{
  "everything": "large transcript, tool logs, code, and unrelated notes..."
}
```

## Compose answers in code

### Speculative fan-out

Ask every question your code might need for the same state in one request. Code can ignore answers from branches it does not take.

### Second requests

Make a second request only when code cannot build it without a first answer, such as when the first answer determines what data to fetch.

### Confidence-gated routing

The answer says what. Confidence says whether to act.

Use three paths:

- act automatically when confidence is high and the action is low risk,
- ask for confirmation or flag for review when confidence is medium,
- hand off or refuse to act when confidence is low and cost of error is high.

Start conservative:

- floor around 0.5 to 0.6 for low-risk routing,
- 0.85 to 0.9 for high-stakes local actions.

Tune thresholds on labeled examples.

### Composite scoring

Split complex judgments into one `score` per dimension. Normalize each score by `len(criteria) - 1`, then combine with weights in code. Change weights when policy changes. Do not rewrite questions just to change policy.

### Intent routing

Classify intent with `choice`, add a risk or complexity `score`, then route to deterministic code, a specialist model, or a person.

### Taxonomy walk

Ask one `choice` per tree level. Give each option its subtree or direct children in criteria. Follow multiple branches when probabilities are close.

### Counting

Laya does not count. Ask one `noul` per item and sum probabilities or thresholded answers in code.

### Dates

Extract date parts with `choice` questions over enumerated options, including `not_stated`. Assemble and compare dates in code.

### Extraction

Generate candidates with code, regex, search, or a generative model. Ask Laya to pick among candidates with `choice`, or verify one candidate with `noul`.

## Read answers

- `choice` returns one selected option and a probability per option.
- `score` returns the probability-weighted mean of level indexes.
- `score` also returns the level distribution; read it when decisions are close.
- `confidence` measures how peaked the distribution is. It is not a guarantee of correctness.
- `noul` returns probability of true. Distance from 0.5 is the confidence-like signal.
- Every answer stays inside the options you supplied. Code should not parse prose.

Important score caveat:

A score of `1.0` can mean certainty on level 1, or an even split between levels 0 and 2. Always inspect `probabilities` when the action matters.

## Improve a Laya program

Find the failing question before changing anything. Collect labeled examples, run them, and compare each answer and probability distribution against labels.

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Wrong answers with high confidence | Laya read the instruction literally | State the exact condition and boundary case |
| Low confidence on a choice | Options overlap or no option fits | Add `not_for`, examples, or `other` |
| Low confidence on a score | Levels overlap or measure multiple dimensions | Rewrite distinct levels or split the question |
| Scores cluster in the middle | Levels are vague degrees | Describe concrete situations |
| Top-of-scale cases look alike | Extreme case has no level | Add a level for the extreme |
| Noul hovers near 0.5 | Condition is vague | Define true/false criteria |
| Accuracy falls as state grows | State carries irrelevant detail | Filter and shrink state |
| Errors on counts, sums, dates, or numeric nearness | Laya is doing arithmetic | Move arithmetic to code |
| Errors on nested or negated questions | Too much indirection | Ask the direct question |
| Answer follows text inside state | State content steers the model | Tighten criteria and gate actions |
| Final decision is wrong while answers are right | Policy composition is wrong | Change thresholds or weights in code |
| Program is slow or costly | Questions are spread over serial calls | Merge same-state questions |

Revision rules:

- Change one or two questions per revision.
- Judge revisions on labeled data, not confidence alone.
- Keep answer spaces stable once code depends on them.
- Put general rules in instructions and criteria.
- Put specific examples in `examples`.

## Bot guard pattern

For local assistant safety, split intent, risk, and confirmation:

```python
GUARD_QUESTIONS = {
    "intent": {
        "type": "choice",
        "instructions": "What does the user want the local assistant to do?",
        "criteria": {
            "chat": "general conversation or question answering",
            "read_file": "read a local file",
            "write_file": "create, edit, or overwrite a local file",
            "delete_file": "delete a local file",
            "list_files": "list files in a directory",
            "run_command": "run terminal, shell, package manager, or system commands",
        },
    },
    "risk": {
        "type": "score",
        "instructions": "How risky is this request for the local machine or project state?",
        "criteria": ["safe", "needs caution", "dangerous"],
    },
    "needs_confirmation": {
        "type": "noul",
        "instructions": "Should this request require explicit user confirmation before execution?",
    },
}
```

Composition rule:

```python
if not decision.available:
    continue_without_guard_or_use_safe_default()
elif decision.needs_confirmation or decision.risk >= 1.5:
    ask_user_to_confirm()
else:
    proceed()
```

## Checklist

- [ ] Each question asks one property.
- [ ] A person could answer each question in a second.
- [ ] The primitive matches how code uses the answer.
- [ ] Instructions state the exact condition.
- [ ] Instructions name relevant state paths.
- [ ] Criteria agree with the instruction.
- [ ] Choice options are contrastive and include `other` when needed.
- [ ] Score levels are concrete, ordered, and one-dimensional.
- [ ] Noul questions have crisp true/false boundaries.
- [ ] Code handles counting, arithmetic, date comparison, and policy.
- [ ] State includes only relevant fields.
- [ ] Same-state questions are batched.
- [ ] Every action has a threshold matched to risk.
- [ ] Low confidence has a fallback path.
- [ ] Labeled examples back every revision.

## Sources

- https://github.com/receptron/laya
- https://raw.githubusercontent.com/receptron/laya/main/README.md
