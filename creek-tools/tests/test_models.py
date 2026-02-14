"""Tests for creek.models module — Pydantic models for ontological primitives."""

import json
from datetime import date, datetime

import pytest
from pydantic import ValidationError

from creek.models import (
    Color,
    Confidence,
    Decision,
    DecisionStatus,
    Dosage,
    Eddy,
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    Mode,
    Orientation,
    Phase,
    Praxis,
    PraxisPotential,
    PraxisStatus,
    PraxisType,
    ReviewInterval,
    SourcePlatform,
    Thread,
    ThreadStatus,
    VoiceClassification,
    VoiceRegister,
    WavelengthClassification,
    WavelengthObservation,
)

# ---- Enum Tests ----


class TestFrequencyEnum:
    """Tests for the Frequency enum."""

    def test_all_frequencies_exist(self) -> None:
        """Verify all 11 frequency values exist."""
        expected = [
            "F1",
            "F2",
            "F3",
            "F4",
            "F5",
            "F6",
            "F7",
            "F8",
            "F9",
            "F10",
            "unclassified",
        ]
        actual = [f.value for f in Frequency]
        assert actual == expected

    def test_frequency_is_string_enum(self) -> None:
        """Frequency values should be usable as strings."""
        assert Frequency.F1 == "F1"
        assert Frequency.UNCLASSIFIED == "unclassified"


class TestPhaseEnum:
    """Tests for the Phase enum."""

    def test_all_phases_exist(self) -> None:
        """Verify all 7 phase values exist."""
        expected = [
            "rising",
            "peaking",
            "withdrawal",
            "diminishing",
            "bottoming_out",
            "restoration",
            "unclassified",
        ]
        actual = [p.value for p in Phase]
        assert actual == expected


class TestModeEnum:
    """Tests for the Mode enum."""

    def test_all_modes_exist(self) -> None:
        """Verify all 6 mode values exist."""
        expected = [
            "inhabit",
            "express",
            "collaborate",
            "integrate",
            "absorb",
            "unclassified",
        ]
        actual = [m.value for m in Mode]
        assert actual == expected


class TestOrientationEnum:
    """Tests for the Orientation enum."""

    def test_all_orientations_exist(self) -> None:
        """Verify all 4 orientation values exist."""
        expected = ["do", "feel", "do_feel", "unclassified"]
        actual = [o.value for o in Orientation]
        assert actual == expected


class TestDosageEnum:
    """Tests for the Dosage enum."""

    def test_all_dosages_exist(self) -> None:
        """Verify all 4 dosage values exist."""
        expected = ["medicine", "toxic", "ambiguous", "unclassified"]
        actual = [d.value for d in Dosage]
        assert actual == expected


class TestColorEnum:
    """Tests for the Color enum."""

    def test_all_colors_exist(self) -> None:
        """Verify all 11 color values exist."""
        expected = [
            "beige",
            "purple",
            "red",
            "blue",
            "orange",
            "green",
            "yellow",
            "teal",
            "ultraviolet",
            "clear_light",
            "unclassified",
        ]
        actual = [c.value for c in Color]
        assert actual == expected


class TestVoiceRegisterEnum:
    """Tests for the VoiceRegister enum."""

    def test_all_registers_exist(self) -> None:
        """Verify all 7 voice register values exist."""
        expected = [
            "confessional",
            "analytical",
            "playful",
            "prophetic",
            "instructional",
            "raw",
            "conversational",
        ]
        actual = [v.value for v in VoiceRegister]
        assert actual == expected


class TestConfidenceEnum:
    """Tests for the Confidence enum."""

    def test_all_confidence_levels_exist(self) -> None:
        """Verify all 5 confidence levels exist."""
        expected = ["musing", "exploring", "forming", "settled", "conviction"]
        actual = [c.value for c in Confidence]
        assert actual == expected


class TestPraxisTypeEnum:
    """Tests for the PraxisType enum."""

    def test_all_praxis_types_exist(self) -> None:
        """Verify all 5 praxis types exist."""
        expected = ["habit", "practice", "framework", "insight", "commitment"]
        actual = [p.value for p in PraxisType]
        assert actual == expected


class TestPraxisStatusEnum:
    """Tests for the PraxisStatus enum."""

    def test_all_praxis_statuses_exist(self) -> None:
        """Verify all 4 praxis statuses exist."""
        expected = ["proposed", "active", "integrated", "released"]
        actual = [p.value for p in PraxisStatus]
        assert actual == expected


class TestReviewIntervalEnum:
    """Tests for the ReviewInterval enum."""

    def test_all_intervals_exist(self) -> None:
        """Verify all 5 review interval values exist."""
        expected = ["daily", "weekly", "monthly", "seasonal", "as_needed"]
        actual = [r.value for r in ReviewInterval]
        assert actual == expected


class TestThreadStatusEnum:
    """Tests for the ThreadStatus enum."""

    def test_all_thread_statuses_exist(self) -> None:
        """Verify all 3 thread statuses exist."""
        expected = ["active", "dormant", "resolved"]
        actual = [t.value for t in ThreadStatus]
        assert actual == expected


class TestDecisionStatusEnum:
    """Tests for the DecisionStatus enum."""

    def test_all_decision_statuses_exist(self) -> None:
        """Verify all 5 decision statuses exist."""
        expected = [
            "sensing",
            "deliberating",
            "committing",
            "enacted",
            "reflecting",
        ]
        actual = [d.value for d in DecisionStatus]
        assert actual == expected


class TestPraxisPotentialEnum:
    """Tests for the PraxisPotential enum."""

    def test_all_praxis_potentials_exist(self) -> None:
        """Verify all 3 praxis potentials exist."""
        expected = ["none", "latent", "explicit"]
        actual = [p.value for p in PraxisPotential]
        assert actual == expected


class TestSourcePlatformEnum:
    """Tests for the SourcePlatform enum."""

    def test_all_platforms_exist(self) -> None:
        """Verify all 9 source platform values exist."""
        expected = [
            "claude",
            "chatgpt",
            "discord",
            "journal",
            "essay",
            "code",
            "email",
            "image_ocr",
            "other",
        ]
        actual = [s.value for s in SourcePlatform]
        assert actual == expected


# ---- Nested Model Tests ----


class TestFragmentSource:
    """Tests for the FragmentSource nested model."""

    def test_defaults(self) -> None:
        """FragmentSource with only platform should have None for optional fields."""
        source = FragmentSource(platform=SourcePlatform.CLAUDE)
        assert source.platform == "claude"
        assert source.original_file is None
        assert source.original_encoding is None
        assert source.conversation_id is None
        assert source.channel is None
        assert source.interlocutor is None

    def test_full_data(self) -> None:
        """FragmentSource with all fields should serialize correctly."""
        source = FragmentSource(
            platform=SourcePlatform.DISCORD,
            original_file="chat.txt",
            original_encoding="utf-8",
            conversation_id="conv-123",
            channel="#general",
            interlocutor="user42",
        )
        dump = source.model_dump()
        assert dump["platform"] == "discord"
        assert dump["original_file"] == "chat.txt"
        assert dump["interlocutor"] == "user42"

    def test_enum_serialized_as_string(self) -> None:
        """Platform enum should serialize as a string in model_dump."""
        source = FragmentSource(platform=SourcePlatform.JOURNAL)
        dump = source.model_dump()
        assert isinstance(dump["platform"], str)
        assert dump["platform"] == "journal"


class TestFrequencyClassification:
    """Tests for the FrequencyClassification nested model."""

    def test_defaults(self) -> None:
        """Default FrequencyClassification should be unclassified with empty list."""
        fc = FrequencyClassification()
        assert fc.primary == "unclassified"
        assert fc.secondary == []

    def test_with_values(self) -> None:
        """FrequencyClassification with explicit values should work."""
        fc = FrequencyClassification(
            primary=Frequency.F3,
            secondary=[Frequency.F5, Frequency.F7],
        )
        assert fc.primary == "F3"
        assert fc.secondary == ["F5", "F7"]

    def test_dump_produces_strings(self) -> None:
        """Enum values should be strings after model_dump."""
        fc = FrequencyClassification(
            primary=Frequency.F1,
            secondary=[Frequency.F2],
        )
        dump = fc.model_dump()
        assert dump["primary"] == "F1"
        assert dump["secondary"] == ["F2"]


class TestWavelengthClassification:
    """Tests for the WavelengthClassification nested model."""

    def test_defaults(self) -> None:
        """Default WavelengthClassification should be fully unclassified."""
        wc = WavelengthClassification()
        assert wc.phase == "unclassified"
        assert wc.mode == "unclassified"
        assert wc.orientation == "unclassified"
        assert wc.dosage == "unclassified"
        assert wc.color == "unclassified"
        assert wc.descriptor == ""

    def test_with_values(self) -> None:
        """WavelengthClassification with explicit values should work."""
        wc = WavelengthClassification(
            phase=Phase.RISING,
            mode=Mode.EXPRESS,
            orientation=Orientation.DO,
            dosage=Dosage.MEDICINE,
            color=Color.RED,
            descriptor="intense creative surge",
        )
        assert wc.phase == "rising"
        assert wc.mode == "express"
        assert wc.descriptor == "intense creative surge"


class TestVoiceClassification:
    """Tests for the VoiceClassification nested model."""

    def test_defaults(self) -> None:
        """Default VoiceClassification should have None for optional fields."""
        vc = VoiceClassification()
        assert vc.voice_register is None
        assert vc.confidence is None

    def test_with_values(self) -> None:
        """VoiceClassification with explicit values should work."""
        vc = VoiceClassification(
            voice_register=VoiceRegister.CONFESSIONAL,
            confidence=Confidence.SETTLED,
        )
        assert vc.voice_register == "confessional"
        assert vc.confidence == "settled"


# ---- Primitive Model Tests ----


class TestFragment:
    """Tests for the Fragment model."""

    def test_minimal_creation(self) -> None:
        """Fragment with only title should use defaults for everything else."""
        frag = Fragment(
            title="Test Fragment",
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
        )
        assert frag.type == "fragment"
        assert frag.title == "Test Fragment"
        assert frag.id.startswith("frag-")
        assert len(frag.id) == 13  # "frag-" + 8 hex chars
        assert frag.frequency.primary == "unclassified"
        assert frag.wavelength.phase == "unclassified"
        assert frag.voice.voice_register is None
        assert frag.emotional_texture == []
        assert frag.threads == []
        assert frag.eddies == []
        assert frag.praxis_potential == "none"
        assert frag.tags == []

    def test_full_creation(self) -> None:
        """Fragment with all fields specified should work."""
        now = datetime(2024, 1, 15, 10, 30, 0)
        frag = Fragment(
            title="Deep Insight",
            source=FragmentSource(
                platform=SourcePlatform.JOURNAL,
                original_file="diary.md",
            ),
            created=now,
            ingested=now,
            frequency=FrequencyClassification(
                primary=Frequency.F7,
                secondary=[Frequency.F3],
            ),
            wavelength=WavelengthClassification(
                phase=Phase.PEAKING,
                mode=Mode.INTEGRATE,
                orientation=Orientation.FEEL,
                dosage=Dosage.MEDICINE,
                color=Color.YELLOW,
                descriptor="integration moment",
            ),
            voice=VoiceClassification(
                voice_register=VoiceRegister.ANALYTICAL,
                confidence=Confidence.CONVICTION,
            ),
            emotional_texture=["awe", "clarity"],
            threads=["thread-abc12345"],
            eddies=["eddy-def67890"],
            praxis_potential=PraxisPotential.EXPLICIT,
            tags=["insight", "integration"],
        )
        assert frag.title == "Deep Insight"
        assert frag.frequency.primary == "F7"
        assert frag.wavelength.phase == "peaking"
        assert frag.voice.voice_register == "analytical"
        assert frag.praxis_potential == "explicit"

    def test_id_auto_generation(self) -> None:
        """Each Fragment should get a unique auto-generated ID."""
        frag1 = Fragment(
            title="A",
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
        )
        frag2 = Fragment(
            title="B",
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
        )
        assert frag1.id != frag2.id
        assert frag1.id.startswith("frag-")
        assert frag2.id.startswith("frag-")

    def test_model_dump_serializable(self) -> None:
        """Fragment model_dump should produce a JSON-serializable dict."""
        frag = Fragment(
            title="Test",
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
        )
        dump = frag.model_dump()
        assert isinstance(dump, dict)
        assert dump["type"] == "fragment"
        # Ensure JSON serializable (datetimes need mode="json")
        json_dump = frag.model_dump(mode="json")
        serialized = json.dumps(json_dump)
        assert isinstance(serialized, str)

    def test_round_trip(self) -> None:
        """Create a Fragment, dump it, and recreate from the dict."""
        original = Fragment(
            title="Round Trip Test",
            source=FragmentSource(
                platform=SourcePlatform.ESSAY,
                original_file="essay.md",
            ),
            frequency=FrequencyClassification(primary=Frequency.F5),
            emotional_texture=["curiosity"],
            tags=["test"],
        )
        dump = original.model_dump(mode="json")
        restored = Fragment(**dump)
        assert restored.title == original.title
        assert restored.source.platform == original.source.platform
        assert restored.frequency.primary == original.frequency.primary
        assert restored.emotional_texture == original.emotional_texture
        assert restored.tags == original.tags


class TestThread:
    """Tests for the Thread model."""

    def test_minimal_creation(self) -> None:
        """Thread with only title should use defaults."""
        thread = Thread(title="Test Thread")
        assert thread.type == "thread"
        assert thread.id.startswith("thread-")
        assert len(thread.id) == 15  # "thread-" + 8 hex chars
        assert thread.status == "active"
        assert thread.frequency_affinity == []
        assert thread.fragment_count == 0
        assert thread.description == ""
        assert thread.tags == []

    def test_full_creation(self) -> None:
        """Thread with all fields specified should work."""
        thread = Thread(
            title="Creative Expression",
            status=ThreadStatus.DORMANT,
            first_seen=date(2024, 1, 1),
            last_seen=date(2024, 6, 15),
            frequency_affinity=[Frequency.F3, Frequency.F5],
            fragment_count=42,
            description="Exploring creative outlets",
            tags=["creativity", "expression"],
        )
        assert thread.status == "dormant"
        assert thread.fragment_count == 42
        assert thread.frequency_affinity == ["F3", "F5"]

    def test_id_prefix(self) -> None:
        """Thread ID should have the correct prefix."""
        thread = Thread(title="Test")
        assert thread.id.startswith("thread-")

    def test_model_dump_serializable(self) -> None:
        """Thread model_dump should produce a JSON-serializable dict."""
        thread = Thread(title="Test")
        json_dump = thread.model_dump(mode="json")
        serialized = json.dumps(json_dump)
        assert isinstance(serialized, str)


class TestEddy:
    """Tests for the Eddy model."""

    def test_minimal_creation(self) -> None:
        """Eddy with only title should use defaults."""
        eddy = Eddy(title="Test Eddy")
        assert eddy.type == "eddy"
        assert eddy.id.startswith("eddy-")
        assert len(eddy.id) == 13  # "eddy-" + 8 hex chars
        assert eddy.fragment_count == 0
        assert eddy.threads == []
        assert eddy.description == ""
        assert eddy.tags == []

    def test_full_creation(self) -> None:
        """Eddy with all fields specified should work."""
        eddy = Eddy(
            title="Integration Cluster",
            formed=date(2024, 3, 1),
            fragment_count=15,
            threads=["thread-abc12345", "thread-def67890"],
            description="Cluster around personal integration work",
            tags=["integration", "growth"],
        )
        assert eddy.fragment_count == 15
        assert len(eddy.threads) == 2

    def test_model_dump_serializable(self) -> None:
        """Eddy model_dump should produce a JSON-serializable dict."""
        eddy = Eddy(title="Test")
        json_dump = eddy.model_dump(mode="json")
        serialized = json.dumps(json_dump)
        assert isinstance(serialized, str)


class TestPraxis:
    """Tests for the Praxis model."""

    def test_minimal_creation(self) -> None:
        """Praxis with only title should use defaults."""
        praxis = Praxis(title="Test Praxis")
        assert praxis.type == "praxis"
        assert praxis.id.startswith("praxis-")
        assert len(praxis.id) == 15  # "praxis-" + 8 hex chars
        assert praxis.frequency == []
        assert praxis.praxis_type == "insight"
        assert praxis.derived_from == []
        assert praxis.status == "proposed"
        assert praxis.review_interval == "as_needed"
        assert praxis.tags == []

    def test_full_creation(self) -> None:
        """Praxis with all fields specified should work."""
        praxis = Praxis(
            title="Daily Meditation",
            frequency=[Frequency.F8, Frequency.F9],
            praxis_type=PraxisType.PRACTICE,
            derived_from=["frag-abc12345"],
            status=PraxisStatus.ACTIVE,
            review_interval=ReviewInterval.WEEKLY,
            tags=["meditation", "mindfulness"],
        )
        assert praxis.praxis_type == "practice"
        assert praxis.status == "active"
        assert praxis.frequency == ["F8", "F9"]

    def test_model_dump_serializable(self) -> None:
        """Praxis model_dump should produce a JSON-serializable dict."""
        praxis = Praxis(title="Test")
        json_dump = praxis.model_dump(mode="json")
        serialized = json.dumps(json_dump)
        assert isinstance(serialized, str)


class TestDecision:
    """Tests for the Decision model."""

    def test_minimal_creation(self) -> None:
        """Decision with only title should use defaults."""
        decision = Decision(title="Test Decision")
        assert decision.type == "decision"
        assert decision.id.startswith("decision-")
        assert len(decision.id) == 17  # "decision-" + 8 hex chars
        assert decision.status == "sensing"
        assert decision.decided is None
        assert decision.frequency_context == []
        assert decision.wavelength_phase_at_opening == ""
        assert decision.relevant_threads == []
        assert decision.relevant_praxis == []
        assert decision.options == []
        assert decision.criteria == []
        assert decision.outcome == ""
        assert decision.tags == []

    def test_full_creation(self) -> None:
        """Decision with all fields specified should work."""
        decision = Decision(
            title="Career Change",
            status=DecisionStatus.DELIBERATING,
            opened=date(2024, 2, 1),
            decided=date(2024, 4, 15),
            frequency_context=[Frequency.F5, Frequency.F1],
            wavelength_phase_at_opening="rising",
            relevant_threads=["thread-abc12345"],
            relevant_praxis=["praxis-def67890"],
            options=["stay", "leave", "negotiate"],
            criteria=["fulfillment", "stability", "growth"],
            outcome="negotiate for better role",
            tags=["career", "major-decision"],
        )
        assert decision.status == "deliberating"
        assert decision.decided == date(2024, 4, 15)
        assert len(decision.options) == 3

    def test_model_dump_serializable(self) -> None:
        """Decision model_dump should produce a JSON-serializable dict."""
        decision = Decision(title="Test")
        json_dump = decision.model_dump(mode="json")
        serialized = json.dumps(json_dump)
        assert isinstance(serialized, str)


class TestWavelengthObservation:
    """Tests for the WavelengthObservation model."""

    def test_minimal_creation(self) -> None:
        """WavelengthObservation with only date should use defaults."""
        obs = WavelengthObservation(date=date(2024, 5, 1))
        assert obs.type == "wavelength_observation"
        assert obs.id.startswith("wave-")
        assert len(obs.id) == 13  # "wave-" + 8 hex chars
        assert obs.phase == "unclassified"
        assert obs.mode == "unclassified"
        assert obs.dosage == "unclassified"
        assert obs.confidence == "musing"
        assert obs.notes == ""
        assert obs.fragment_refs == []
        assert obs.tags == []

    def test_full_creation(self) -> None:
        """WavelengthObservation with all fields specified should work."""
        obs = WavelengthObservation(
            date=date(2024, 5, 1),
            phase=Phase.BOTTOMING_OUT,
            mode=Mode.ABSORB,
            dosage=Dosage.TOXIC,
            confidence=Confidence.FORMING,
            notes="Difficult day, everything feels heavy",
            fragment_refs=["frag-abc12345"],
            tags=["low-point", "awareness"],
        )
        assert obs.phase == "bottoming_out"
        assert obs.mode == "absorb"
        assert obs.dosage == "toxic"
        assert obs.confidence == "forming"

    def test_model_dump_serializable(self) -> None:
        """WavelengthObservation model_dump should produce JSON-serializable dict."""
        obs = WavelengthObservation(date=date(2024, 5, 1))
        json_dump = obs.model_dump(mode="json")
        serialized = json.dumps(json_dump)
        assert isinstance(serialized, str)


# ---- Validation Tests ----


class TestValidation:
    """Tests for Pydantic validation on the models."""

    def test_invalid_frequency_rejected(self) -> None:
        """Invalid frequency string should be rejected."""
        with pytest.raises(ValidationError):
            FrequencyClassification(primary="INVALID_FREQ")

    def test_invalid_phase_rejected(self) -> None:
        """Invalid phase string should be rejected."""
        with pytest.raises(ValidationError):
            WavelengthClassification(phase="INVALID_PHASE")

    def test_invalid_platform_rejected(self) -> None:
        """Invalid platform string should be rejected."""
        with pytest.raises(ValidationError):
            FragmentSource(platform="not_a_platform")

    def test_invalid_thread_status_rejected(self) -> None:
        """Invalid thread status should be rejected."""
        with pytest.raises(ValidationError):
            Thread(title="Test", status="not_a_status")

    def test_invalid_praxis_type_rejected(self) -> None:
        """Invalid praxis type should be rejected."""
        with pytest.raises(ValidationError):
            Praxis(title="Test", praxis_type="not_a_type")

    def test_invalid_decision_status_rejected(self) -> None:
        """Invalid decision status should be rejected."""
        with pytest.raises(ValidationError):
            Decision(title="Test", status="not_a_status")


# ---- ID Format Tests ----


class TestIdGeneration:
    """Tests for ID generation across all models."""

    def test_fragment_id_format(self) -> None:
        """Fragment ID should match frag-XXXXXXXX format."""
        frag = Fragment(
            title="Test",
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
        )
        assert frag.id.startswith("frag-")
        hex_part = frag.id[5:]
        assert len(hex_part) == 8
        int(hex_part, 16)  # Should not raise — valid hex

    def test_thread_id_format(self) -> None:
        """Thread ID should match thread-XXXXXXXX format."""
        thread = Thread(title="Test")
        assert thread.id.startswith("thread-")
        hex_part = thread.id[7:]
        assert len(hex_part) == 8
        int(hex_part, 16)

    def test_eddy_id_format(self) -> None:
        """Eddy ID should match eddy-XXXXXXXX format."""
        eddy = Eddy(title="Test")
        assert eddy.id.startswith("eddy-")
        hex_part = eddy.id[5:]
        assert len(hex_part) == 8
        int(hex_part, 16)

    def test_praxis_id_format(self) -> None:
        """Praxis ID should match praxis-XXXXXXXX format."""
        praxis = Praxis(title="Test")
        assert praxis.id.startswith("praxis-")
        hex_part = praxis.id[7:]
        assert len(hex_part) == 8
        int(hex_part, 16)

    def test_decision_id_format(self) -> None:
        """Decision ID should match decision-XXXXXXXX format."""
        decision = Decision(title="Test")
        assert decision.id.startswith("decision-")
        hex_part = decision.id[9:]
        assert len(hex_part) == 8
        int(hex_part, 16)

    def test_wavelength_observation_id_format(self) -> None:
        """WavelengthObservation ID should match wave-XXXXXXXX format."""
        obs = WavelengthObservation(date=date(2024, 1, 1))
        assert obs.id.startswith("wave-")
        hex_part = obs.id[5:]
        assert len(hex_part) == 8
        int(hex_part, 16)

    def test_ids_are_unique(self) -> None:
        """Multiple instances of the same model should have unique IDs."""
        ids = {Thread(title="Test").id for _ in range(100)}
        assert len(ids) == 100


# ---- model_dump Enum Serialization Tests ----


class TestEnumSerialization:
    """Tests that model_dump produces plain strings for all enum fields."""

    def test_fragment_enums_as_strings(self) -> None:
        """Fragment model_dump should serialize all enums as strings."""
        frag = Fragment(
            title="Test",
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
            frequency=FrequencyClassification(primary=Frequency.F1),
            wavelength=WavelengthClassification(phase=Phase.RISING),
            praxis_potential=PraxisPotential.LATENT,
        )
        dump = frag.model_dump()
        assert isinstance(dump["type"], str)
        assert isinstance(dump["praxis_potential"], str)
        assert isinstance(dump["source"]["platform"], str)
        assert isinstance(dump["frequency"]["primary"], str)
        assert isinstance(dump["wavelength"]["phase"], str)

    def test_thread_enums_as_strings(self) -> None:
        """Thread model_dump should serialize status as string."""
        thread = Thread(
            title="Test",
            status=ThreadStatus.DORMANT,
            frequency_affinity=[Frequency.F2],
        )
        dump = thread.model_dump()
        assert isinstance(dump["status"], str)
        assert dump["status"] == "dormant"
        assert all(isinstance(f, str) for f in dump["frequency_affinity"])

    def test_wavelength_observation_enums_as_strings(self) -> None:
        """WavelengthObservation dump should serialize all enums as strings."""
        obs = WavelengthObservation(
            date=date(2024, 1, 1),
            phase=Phase.RESTORATION,
            mode=Mode.INHABIT,
            dosage=Dosage.MEDICINE,
            confidence=Confidence.EXPLORING,
        )
        dump = obs.model_dump()
        assert isinstance(dump["phase"], str)
        assert isinstance(dump["mode"], str)
        assert isinstance(dump["dosage"], str)
        assert isinstance(dump["confidence"], str)
