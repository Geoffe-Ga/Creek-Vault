"""Cross-cutting care-boundary policy for Creek's voice-generating tools (#753).

Houses the shared medication/care policy woven into every voice prompt and the
conservative acute-distress detector that backs the reflect care seam. Lives in
``creek/`` (not ``creek_mcp/``) so the core generators (draft / author / compile)
can import it without depending on the MCP layer.
"""

from creek.care.guardrail import CARE_POLICY, CARE_SIGNAL, acute_distress_guard

__all__ = ["CARE_POLICY", "CARE_SIGNAL", "acute_distress_guard"]
