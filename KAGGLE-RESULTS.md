# What it found in top-3 and gold-medal Kaggle solutions

We pointed it at five public solutions to Kaggle competitions, each a top-three finish or a gold
medal, on 2026-10-12. It confirmed **189 defects, 2 of them critical and 22 major**. Every one
was re-checked by the reconciler before it was reported: commands re-run, quotes re-read at their
lines, and in places the code re-executed on the committed files or on synthetic inputs. None of
the competition datasets is in these repositories, so no model was retrained and no score
reproduced. How much each defect moved a score is unmeasured.

The solutions are not named. The point is what a careful review finds in strong, public work
that people read to learn how it is done, not who wrote it. None of this changes a standing,
because Kaggle scored each one on a hidden test set. What it changes is how far the numbers
inside each repository can be trusted.

The letters match the cost table in the README, smallest repository first.

**Solution A** (693 reviewable lines). 14 defects, 1 critical, 0 major.
- One model's probability for whichever class it predicts is scored as the probability of the
  positive class, so every row it predicts as negative is scored with its probabilities swapped.
  That model's loss and two ensemble losses in the model comparison are invalid, and the choice
  of which ensemble to ship may rest on them.
- The negative class is undersampled once, before cross-validation, so every fold comes from the
  same random draw, the dropped rows are never scored, and nothing shows how the results move
  with the draw.
- The scaler and encoder are fitted on the whole training set before the folds are split, so each
  validation fold helps shape the scaling its model is trained with.

**Solution B** (1,397 reviewable lines). 36 defects, 0 critical, 4 major.
- The hyperparameter search scores one fixed configuration and never sees the grid.
- k-means cluster labels are assigned to the wrong rows in cross-validation and stacking, up to
  every row of a fold. The submission path assigns them correctly, so the models that were
  scored and the models that were shipped saw differently built features.
- The shipped models get an extra feature that cross-validation never sees: each row's position
  in its own file, added by accident when train and test are joined.
- Two target encodings encode how many rows share each ID, not the ID itself, because the IDs
  were replaced by their counts first.

**Solution C** (2,921 reviewable lines). 49 defects, 1 critical, 9 major.
- Negative rows are duplicated before the train/validation split, so 72% of the validation rows
  have an exact copy in training. Every validation score is close to a score on training data.
- The text cleaning corrupts ordinary words: a replacement rule with no word boundaries rewrites
  the middle of common words, and a typo rule turns one common word into another. The main
  pipeline builds its features from the cleaned text.
- The README's final scores trace to no script. There is no code for the two-model blend it
  reports, and nothing writes the feature files the models read, so no reported score can be
  rebuilt from the repository.
- The tuning script picks its number of boosting rounds by the training loss, which falls every
  round, so it always returns the last round and the cross-validation is thrown away.

**Solution D** (3,744 reviewable lines). 48 defects, 0 critical, 5 major.
- One model in the ensemble never sees its own label file. The code always loads the default
  file and ignores the one its config names, so the model trains on the wrong set of classes,
  and its handling of mirrored images marks the wrong class.
- The code that combines five detectors by picking the two that agree most closely compares
  each model with itself, and a model always agrees with itself perfectly. On the committed
  files, every test row of the "ensemble" is one model's output.
- The same script stores half of each box's size as the box's centre, which pins every box to the
  image's top-left corner, so the crops the downstream models train on are shifted toward that
  corner.
- The detector's box-overlap score multiplies one box's width by the other box's height, so it
  is not the overlap it is named for. It decides which anchors count as hits in training, and
  which checkpoint is kept.

**Solution E** (4,794 reviewable lines). 42 defects, 0 critical, 4 major.
- The prerender step and the training reader use different names for the validation set, so
  every first-level training run crashes before its first epoch.
- The script for downloading the trained first-level weights downloads nothing: its only download
  line is commented out. With training broken too, the repository's own scripts can neither
  retrain nor download the first-level models; only their predictions are still linked.
- First-level training is entirely unseeded.
- Second-level submission rows are labelled by position from the sample submission, and nothing
  checks that they line up. The inputs are ordered by sorted file names, where "10000" comes
  before "9999".
