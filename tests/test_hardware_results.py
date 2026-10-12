import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("validator", ROOT / "validate-results.py")
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


class HardwareResultsTest(unittest.TestCase):
    def validate(self, filename, runner, directory="results/2026/09/21"):
        record = {
            "schema_version": 1,
            "run": {
                "run_id": "123",
                "run_attempt": 1,
                "workflow": "building-comfort-azure.yml",
                "runner": runner,
                "started_at": "2026-09-21T07:00:00Z",
            },
            "versions": {"test_infra_sha": "a" * 40},
            "dimensions": {"scenario": "building_comfort", "variant": "drasi_lib"},
            "status": "failure",
            "determinism": "not_applicable",
            "reactions": [],
        }
        report = validator.Report()
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / filename
            path.write_text(json.dumps(record))
            validator.validate_file(report, path, Path(directory) / filename)
        return report.errors

    def test_github_runner_folder(self):
        self.assertEqual(self.validate("building_comfort__drasi_lib__123.json", "ubuntu-latest", "results/ubuntu-latest/2026/09/21"), [])

    def test_runner_folder_mismatch(self):
        errors = self.validate("building_comfort__drasi_lib__123.json", "azure-ephemeral-Standard_D4s_v6", "results/ubuntu-latest/2026/09/21")
        self.assertTrue(any("run.runner disagrees with folder" in error for error in errors))

    def test_runner_folder_date_mismatch(self):
        errors = self.validate("building_comfort__drasi_lib__123.json", "ubuntu-latest", "results/ubuntu-latest/2026/09/20")
        self.assertTrue(any("path date" in error for error in errors))

    def test_hardware_profiles(self):
        for sku in ("Standard_D4s_v3", "Standard_D4s_v4", "Standard_D4s_v6", "Standard_F4as_v7"):
            with self.subTest(sku=sku):
                runner = f"azure-ephemeral-{sku}-Premium_LRS-128gb"
                self.assertEqual(self.validate(f"building_comfort__drasi_lib__{runner}__123.json", runner), [])

    def test_sku_folders(self):
        for sku in ("Standard_D4s_v3", "Standard_D4s_v4", "Standard_D4s_v6", "Standard_F4as_v7"):
            for disk in ("Premium_LRS-128gb", "StandardSSD_LRS-256gb"):
                with self.subTest(sku=sku, disk=disk):
                    runner = f"azure-ephemeral-{sku}-{disk}"
                    self.assertEqual(self.validate(
                        f"building_comfort__drasi_lib__{runner}__123.json",
                        runner, f"results/{sku}/2026/09/21",
                    ), [])

    def test_sku_folder_mismatch(self):
        for runner in (
            "ubuntu-latest",
            "Standard_D4s_v4",
            "azure-ephemeral-Standard_D4s_v6-Premium_LRS-128gb",
            "azure-ephemeral-Standard_D4s_v40-Premium_LRS-128gb",
            None,
        ):
            with self.subTest(runner=runner):
                errors = self.validate(
                    "building_comfort__drasi_lib__123.json",
                    runner, "results/Standard_D4s_v4/2026/09/21",
                )
                self.assertTrue(any("run.runner disagrees with folder" in error for error in errors))

    def test_sku_folder_date_mismatch(self):
        errors = self.validate(
            "building_comfort__drasi_lib__123.json",
            "azure-ephemeral-Standard_D4s_v4", "results/Standard_D4s_v4/2026/09/20",
        )
        self.assertTrue(any("path date" in error for error in errors))

    def test_sku_folder_filename_mismatch(self):
        errors = self.validate(
            "building_comfort__drasi_lib__azure-ephemeral-Standard_D4s_v6__123.json",
            "azure-ephemeral-Standard_D4s_v4", "results/Standard_D4s_v4/2026/09/21",
        )
        self.assertTrue(any("run.runner disagrees with filename" in error for error in errors))

    def test_invalid_runner_folder(self):
        for folder in ("Standard_D4s_v4!", "Standard_", "unknown", "Standard_D4s_v4-extra"):
            with self.subTest(folder=folder):
                errors = self.validate(
                    "building_comfort__drasi_lib__123.json",
                    "azure-ephemeral-Standard_D4s_v4", f"results/{folder}/2026/09/21",
                )
                self.assertTrue(any("results files must live at" in error for error in errors))

    def test_legacy_filename_in_sku_folder(self):
        self.assertEqual(self.validate(
            "building_comfort__drasi_lib__123.json",
            "azure-ephemeral-Standard_D4s_v4", "results/Standard_D4s_v4/2026/09/21",
        ), [])

    def test_legacy_filename(self):
        self.assertEqual(self.validate("building_comfort__drasi_lib__123.json", "ubuntu-latest"), [])

    def test_mismatched_profile(self):
        errors = self.validate("building_comfort__drasi_lib__azure-ephemeral-Standard_D4s_v6__123.json", "ubuntu-latest")
        self.assertTrue(any("run.runner disagrees" in error for error in errors))

    def test_invalid_profile(self):
        errors = self.validate("building_comfort__drasi_lib__azure-ephemeral-invalid!__123.json", "invalid")
        self.assertTrue(any("filename must" in error for error in errors))

    def test_mismatched_run_id(self):
        errors = self.validate("building_comfort__drasi_lib__azure-ephemeral-test__456.json", "azure-ephemeral-test")
        self.assertTrue(any("run.run_id" in error for error in errors))


if __name__ == "__main__":
    unittest.main()