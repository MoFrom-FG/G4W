from G4W.knowledge import paths


def test_knowledge_paths_are_separate(monkeypatch, tmp_path):
    monkeypatch.setenv("G4W_DATA_DIR", str(tmp_path / "G4W-data"))
    monkeypatch.setenv("G4W_VECTOR_INDEX_DIR", str(tmp_path / "G4W-vector-index"))
    assert paths.data_root().name == "knowledge"
    assert paths.vector_root().name == "knowledge"
    assert "memory" not in str(paths.data_root()).lower()
    assert "memory" not in str(paths.vector_root()).lower()


def test_knowledge_paths_follow_bind_workspace_root(monkeypatch, tmp_path):
    portable_root = tmp_path / "moved-g4w"
    monkeypatch.delenv("G4W_KNOWLEDGE_DATA_DIR", raising=False)
    monkeypatch.delenv("G4W_KNOWLEDGE_VECTOR_INDEX_DIR", raising=False)
    monkeypatch.delenv("G4W_DATA_DIR", raising=False)
    monkeypatch.delenv("G4W_STATE_DIR", raising=False)
    monkeypatch.delenv("G4W_VECTOR_INDEX_DIR", raising=False)
    monkeypatch.delenv("G4W_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("BBS_CWD", raising=False)
    monkeypatch.setenv("G4W_WORKSPACE_ROOT", str(portable_root))

    assert paths.data_root() == (portable_root / "runtime" / "G4W-data").resolve() / "knowledge"
    assert paths.vector_root() == (portable_root / "runtime" / "G4W-vector-index").resolve() / "knowledge"


def test_knowledge_data_prefers_launch_state_dir(monkeypatch, tmp_path):
    portable_root = tmp_path / "g4w"
    state_dir = portable_root / "runtime" / "G4W-data"
    monkeypatch.delenv("G4W_KNOWLEDGE_DATA_DIR", raising=False)
    monkeypatch.delenv("G4W_DATA_DIR", raising=False)
    monkeypatch.setenv("G4W_WORKSPACE_ROOT", str(portable_root))
    monkeypatch.setenv("G4W_STATE_DIR", str(state_dir))

    assert paths.data_root() == state_dir / "knowledge"
