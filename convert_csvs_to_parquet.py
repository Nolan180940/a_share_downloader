import sys
from pathlib import Path
import traceback

def main():
    try:
        import yaml
        import pandas as pd
    except Exception as e:
        print("Missing dependencies:", e)
        sys.exit(2)

    repo_root = Path(__file__).resolve().parent
    cfg_file = repo_root / "config.yaml"
    if cfg_file.exists():
        cfg = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        data_dir = Path(cfg.get("data_dir", repo_root / "data"))
    else:
        data_dir = repo_root / "data"

    data_dir = Path(str(data_dir))
    if not data_dir.exists():
        print("Data directory not found:", data_dir)
        sys.exit(1)

    csv_files = sorted(data_dir.glob("*.csv"))
    total = len(csv_files)
    if total == 0:
        print("No CSV files found in", data_dir)
        return

    converted = 0
    skipped = 0
    failed = 0

    for f in csv_files:
        try:
            print(f"Converting {f.name}...")
            df = pd.read_csv(f, parse_dates=["date"], low_memory=False, encoding="utf-8")
            out = f.with_suffix(".parquet")
            try:
                df.to_parquet(out, engine="pyarrow", index=False, compression="snappy")
            except Exception:
                df.to_parquet(out, engine="fastparquet", index=False, compression="snappy")
            # verify
            if out.exists() and out.stat().st_size > 0:
                f.unlink()
                converted += 1
            else:
                print(f"Parquet not created for {f}, skipping deletion")
                skipped += 1
        except Exception:
            failed += 1
            print(f"Failed to convert {f}")
            traceback.print_exc()

    print(f"Done. total={total}, converted={converted}, skipped={skipped}, failed={failed}")

if __name__ == "__main__":
    main()
