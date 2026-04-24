# centris_runner

This folder wraps the CENTRIS Docker workflow in one script.

## Layout

Put the CENTRIS dataset tarball here:

```text
centris_runner/
  run_centris.sh
  data/
    Centris_dataset.tar
  output/
```

The script automatically extracts `data/Centris_dataset.tar` when it cannot find an extracted dataset.

It also accepts an already-extracted dataset under `data/` as long as the dataset root contains:

```text
componentDB/
metaInfos/
```

Optional directories are also mounted when present:

```text
aveFuncs/
weights/
funcDate/
verIDX/
```

## Data
https://zenodo.org/records/4514689#.YB7sN-gzaUk
