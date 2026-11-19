import json
import os

import pytest

from company_bid.provider import ProviderFailure
from company_bid.reading import (
    ReadingConfig, ReadingUnit, SourceDocument, UnitCoverage, _UnitFailure, _resolve_unread,
)


@pytest.mark.parametrize("first_error,second_called", [
    ("OCR_REVIEWED_TEXT_MISSING", True),
    ("OCR_TRANSPORT_OUTCOME_UNKNOWN", False),
])
def test_failed_region_keeps_its_source_and_only_independent_regions_continue(tmp_path, first_error, second_called):
    source = SourceDocument(source_id="T", title="Synthetic", path=tmp_path / "source.pdf", role="tender",
                            sha256="0" * 64)
    unit = ReadingUnit(unit_id="page", source_id="T", locator="pdf/page[1]", page=1,
                       coverage=UnitCoverage(status="needs_ocr"))
    locators = [unit.locator + "/image[0]", unit.locator + "/image[1]"]
    seen = []

    def recognize(request):
        seen.append(request["locator"])
        if request["locator"] == locators[0]:
            raise ProviderFailure(first_error)
        return {**{key: request[key] for key in ("source_id", "unit_id", "locator", "page")},
                "text": "Second figure only"}

    recognize.independent_object_errors = {"OCR_REVIEWED_TEXT_MISSING"}
    with pytest.raises(_UnitFailure) as failed:
        _resolve_unread(source, unit, ReadingConfig(ocr_version="synthetic"), recognize,
                        [(locator, {"object_kind": "pdf_region"}) for locator in locators])
    assert failed.value.code == first_error and unit.coverage.status == "failed"
    assert seen == (locators if second_called else locators[:1])
    assert unit.coverage.unread_objects == (locators[:1] if second_called else locators)
    assert [b.text for b in unit.blocks] == (["Second figure only"] if second_called else [])


def test_cli_credential_file_uses_official_ocr_env_for_prepared_writing(tmp_path, monkeypatch, capsys):
    from company_bid import cli

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"pipeline": "automatic", "provider": {"mode": "prepared"},
                                  "ocr": {"engine": "deepseek_ocr"}}), encoding="utf-8")
    credential = tmp_path / "key"
    credential.write_text("synthetic-ocr-credential", encoding="utf-8")
    monkeypatch.delenv("COMPANY_BID_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("COMPANY_BID_API_KEY", "other-synthetic-provider")

    class Probe:
        def inspect(self, run_id):
            assert os.environ["COMPANY_BID_DEEPSEEK_API_KEY"] == "synthetic-ocr-credential"
            assert os.environ["COMPANY_BID_API_KEY"] == "other-synthetic-provider"
            return {"run_id": run_id, "status": "pending"}

    monkeypatch.setattr(cli, "build_executor", lambda *args: Probe())
    assert cli.main(["--config", str(config), "--credential-file", str(credential), "inspect", "synthetic"]) == 0
    stream = capsys.readouterr()
    assert "synthetic-ocr-credential" not in stream.out + stream.err
