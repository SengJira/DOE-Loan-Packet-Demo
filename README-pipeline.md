# Loan-packet processing pipeline

`create_pipeline.py` builds this graph in a Dataloop project:

```
/incoming (dataset node)
   -> classify_document_type                      code node
   -> split by type                               5 filtered edges on metadata.user.doc_type
   -> extract_<doc_type>                          one code node per document type
   -> confidence filter on the edge:
        metadata.user.min_confidence >= 0.75  ->  loan-structured-output   (dataset node)
        metadata.user.min_confidence <  0.75  ->  low_confidence_review    (annotation task)
   -> task action "complete"                  ->  loan-ground-truth        (dataset node)
   -> retrain_trigger                             code node
```

Routing is done with connection conditions (DQL) rather than branching code, so
the type split and the confidence threshold are both readable straight off the
pipeline graph in the console.

## Auto-annotation template

`--template annotate` builds a second pipeline for labelling the `unlabeled`
split automatically:

```
/unlabeled (dataset node)
   -> auto_label                                 code node (writes a Classification annotation)
   -> confidence filter on the edge:
        metadata.user.min_confidence >= 0.75  ->  loan-auto-annotated        (dataset node)
        metadata.user.min_confidence <  0.75  ->  low_confidence_annotation  (annotation task)
   -> task action "complete"                  ->  loan-ground-truth          (dataset node)
   -> retrain_trigger                             code node
```

```bash
python create_pipeline.py --template annotate --name loan-auto-annotation \
  --delete-existing --start
```

`auto_label` attaches a `Classification` annotation with the document type and
sets `min_confidence` the same way `extract_fields` does; swap its body for a
real pre-labeling model. The destination dataset is `--annotated-dataset`
(default `loan-auto-annotated`).

## Run it

```bash
podman run --rm --userns=keep-id \
  -e DATALOOP_PATH=/state -v "$HOME/.dataloop:/state:rw" \
  -v "$PWD:/work:rw" localhost/loan-packet-demo:local \
  python /work/create_pipeline.py --project "My Project"
```

Useful flags:

| flag | default | meaning |
| --- | --- | --- |
| `--source-dataset` | `loan-packets` | dataset holding `/incoming` |
| `--template` | `extract` | `extract` or `annotate` |
| `--source-folder` | `incoming` / `unlabeled` | folder the dataset node watches (`--incoming-folder` still accepted) |
| `--structured-dataset` | `loan-structured-output` | created if missing |
| `--annotated-dataset` | `loan-auto-annotated` | annotate template sink |
| `--ground-truth-dataset` | `loan-ground-truth` | created if missing |
| `--threshold` | `0.75` | confidence that skips human review |
| `--task-owner` | logged-in user | owner/assignee of the review task |
| `--start` | off | install the pipeline (provisions services) |
| `--delete-existing` | off | replace a pipeline of the same name |

The pipeline is created in `Created` state, not started. Starting it provisions
one service per code node, so do that from the console (or with `--start`) when
you actually want the demo running.

## What is a placeholder

Two code nodes carry demo bodies that a real deployment replaces:

* `classify_document_type` reads `metadata.user.doc_type`, which the generator
  already writes, and falls back to a filename match. Swap the body for a
  classification model call.
* `extract_fields` promotes the prelabels the generator produced into
  `metadata.user.extraction` and sets `metadata.user.min_confidence`. Swap the
  body for the extraction model.
* `auto_label` (annotate template) writes a `Classification` annotation per
  item; swap the body for a real pre-labeling model.

`retrain_trigger` counts the ground-truth dataset and logs whether the batch
size (`RETRAIN_BATCH_SIZE`, default 50) has been reached; the `model.train()`
call is commented out until a model is picked.

Only the node bodies are placeholders — the graph, the routing conditions, the
review task and the dataset wiring are real.
