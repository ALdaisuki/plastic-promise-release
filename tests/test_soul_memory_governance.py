from plastic_promise.memory import soul_memory


def test_recmem_default_keeps_mutations_on_python_context_engine(monkeypatch):
    class PythonGovernedEngine:
        pass

    monkeypatch.setattr(soul_memory, "ContextEngine", PythonGovernedEngine)

    rec_mem = soul_memory.RecMem()

    assert isinstance(rec_mem._engine, PythonGovernedEngine)
