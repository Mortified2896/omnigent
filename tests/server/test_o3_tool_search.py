"""Capability intersection must include every possible fallback destination."""

import copy
import json

import pytest

from omnigent.server.o3_routing_review.tool_search import (
    combo_capability,
    qualified_route,
    read_capabilities,
    write_alias_catalog,
)


@pytest.fixture
def contract():
    registry = {
        "schema_version": 1,
        "routes": {
            "provider/primary": {
                "provider": "provider",
                "resolved_model": "native-primary",
                "supports_search_tool": True,
                "search_call": True,
                "loaded_tool_call": True,
                "evidence": "local protocol probe",
            },
            "provider/fallback": {
                "provider": "provider",
                "resolved_model": "native-fallback",
                "supports_search_tool": True,
                "search_call": True,
                "loaded_tool_call": True,
                "evidence": "local protocol probe",
            },
        },
    }
    combo = {
        "name": "custom/o3-route-12345678",
        "computed_context_length": 272000,
        "models": [
            {"kind": "model", "providerId": "provider", "model": route}
            for route in registry["routes"]
        ],
    }
    catalog = {
        "models": [
            {
                "slug": name,
                "supports_search_tool": True,
                "context_window": 900000,
                "tool_mode": "code",
                "base_instructions": "ordinary coding instructions",
            }
            for name in ["native-primary", "native-fallback"]
        ]
    }
    return combo, registry, catalog


def test_alias_intersects_primary_and_fallback_and_preserves_window(contract):
    combo, registry, catalog = contract
    result = combo_capability(combo, registry, catalog)
    assert result["supports_search_tool"] is True
    assert result["context_window"] == 272000
    assert "tool_mode" not in result
    assert result["slug"] == combo["name"]


@pytest.mark.parametrize(
    "field", ["supports_search_tool", "search_call", "loaded_tool_call", "evidence"]
)
def test_fallback_requires_complete_destination_evidence(contract, field):
    combo, registry, catalog = contract
    registry["routes"]["provider/fallback"][field] = False
    assert combo_capability(combo, registry, catalog) is None


def test_capability_does_not_follow_model_name_across_providers(contract):
    combo, registry, catalog = contract
    combo["models"][1]["providerId"] = "different-provider"
    assert combo_capability(combo, registry, catalog) is None
    assert qualified_route(registry, "different-provider", "provider/primary") is None


def test_native_metadata_can_revoke_prior_qualification(contract):
    combo, registry, catalog = contract
    catalog["models"][1]["supports_search_tool"] = False
    assert combo_capability(combo, registry, catalog) is None


@pytest.mark.parametrize(
    "targets",
    [
        [],
        [{"kind": "combo", "model": "another-alias"}],
        [{"kind": "model", "providerId": "unknown", "model": "unqualified"}],
    ],
)
def test_unknown_or_nested_destinations_do_not_inherit_capabilities(contract, targets):
    combo, registry, catalog = contract
    combo["models"] = targets
    assert combo_capability(combo, registry, catalog) is None


def test_private_catalog_preserves_registered_tools_and_original_entries(tmp_path, contract):
    combo, registry, catalog = contract
    before = copy.deepcopy(catalog)
    config = 'model_catalog_json = "/user/catalog.json"\n[mcp_servers.omniroute]\nurl="http://localhost:20128/mcp"\n'
    (tmp_path / "config.toml").write_text(config)
    result = write_alias_catalog(tmp_path, combo, registry, catalog)
    after = json.loads(result["path"].read_text())
    assert after["models"][:-1] == before["models"]
    assert catalog == before
    assert (tmp_path / "config.toml").read_text() == config
    assert result["supports_search_tool"] is True


def test_noncapable_fallback_removes_stale_alias_advertisement(tmp_path, contract):
    combo, registry, catalog = contract
    catalog["models"].append({"slug": combo["name"], "supports_search_tool": True})
    del registry["routes"]["provider/fallback"]
    result = write_alias_catalog(tmp_path, combo, registry, catalog)
    assert result["supports_search_tool"] is False
    assert all(
        row["slug"] != combo["name"] for row in json.loads(result["path"].read_text())["models"]
    )


def test_missing_evidence_disables_optimization_and_corrupt_evidence_fails_closed(tmp_path):
    path = tmp_path / "evidence.json"
    assert read_capabilities(path) is None
    path.write_text('{"schema_version": 7}')
    with pytest.raises(ValueError):
        read_capabilities(path)


@pytest.mark.parametrize("changed", [None, "gateway_version", "gateway_url", "fallback_chains"])
async def test_native_startup_binds_qualification_to_gateway(
    tmp_path, monkeypatch, contract, changed
):
    from omnigent.server.o3_routing_review.omniroute import OmniRouteClient, OmniRouteResponse
    from omnigent.server.o3_routing_review.tool_search import prepare_alias_catalog

    combo, registry, catalog = contract
    registry.update(gateway_url="http://127.0.0.1:20128", gateway_version="test-build")
    if changed in {"gateway_version", "gateway_url"}:
        registry[changed] = "different-gateway"
    evidence = tmp_path / "qualifications.json"
    evidence.write_text(json.dumps(registry))
    monkeypatch.setenv("OMNIGENT_O3_TOOL_SEARCH_CAPABILITIES", str(evidence))
    source = tmp_path / "user-models.json"
    source.write_text(json.dumps(catalog))
    config = (
        f'model_catalog_json = {json.dumps(str(source))}\n[mcp_servers.omniroute]\nurl="local"\n'
    )
    (tmp_path / "config.toml").write_text(config)

    class Client:
        base_url = "http://127.0.0.1:20128"

        async def _request(self, method, path):
            assert method == "GET"
            if path == "/api/fallback/chains":
                return OmniRouteResponse(
                    body={"unknown": []} if changed == "fallback_chains" else {}, headers={}
                )
            assert path == "/api/monitoring/health"
            return OmniRouteResponse(body={"version": "test-build"}, headers={})

        async def get_combo(self, model):
            assert model == combo["name"]
            return combo

    monkeypatch.setattr(OmniRouteClient, "from_env", lambda: Client())
    if changed in {"gateway_version", "gateway_url"}:
        with pytest.raises(ValueError, match="refreshed"):
            await prepare_alias_catalog(tmp_path, combo["name"], "unused")
        assert not (tmp_path / "o3-model-catalog.json").exists()
    else:
        path = await prepare_alias_catalog(tmp_path, combo["name"], "unused")
        models = json.loads(path.read_text())["models"]
        if changed == "fallback_chains":
            assert all(row["slug"] != combo["name"] for row in models)
        else:
            assert models[-1]["supports_search_tool"] is True
    assert json.loads(source.read_text()) == catalog
    assert (tmp_path / "config.toml").read_text() == config
