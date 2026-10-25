# Boundary Red-Team: Technical Data Smuggling Catalogue

**Author:** David Isaac Telles (@Booshdag-io)  
**Slice:** Slice C (Issue #18)  
**Scope:** Hard boundaries on public OpenChokepoint instances.

## 1. Threat Model & Evasion Categories

This attack catalogue categorizes methods an adversarial contributor could use to smuggle controlled technical data into public OpenChokepoint instances:

1. **Engineering Units in Free-Text Fields:** Embedding temperature (`deg C`), pressure (`MPa`, `psi`, `bar`), or velocity (`m/s`, `RPM`) inside `citation_section`, `name`, or `issuer`.
2. **Dimensional Parameters in Identifiers:** Embedding tolerances, clearances, or dimensions (`150mm`, `15 microns`, `tolerance: 0.05`) within `part_number`, `lot_number`, or `serial`.
3. **Smuggling via Citation URLs & Query Strings:** Placing raw measurement values in URL parameters or hash fragments (`https://example.org/spec?temp=1200C&press=200bar`).
4. **Homoglyphs & Unicode Smuggling:** Using look-alike characters (e.g. Cyrillic `а`, invisible zero-width spaces `\u200b`) to bypass ASCII-only regex filters.

## 2. Validator Defenses (RT Series)

- **Rule RT-001:** Enforces recursive pattern scanning across all string and list properties for engineering units, pressure/temperature ranges, and dimensional tolerances. Exits fail-closed with rule tag `RT-001`.

## 3. Residual-Risk Statement

What the validator cannot catch deterministically:
- Natural language descriptions without explicit engineering units (e.g., "heated until cherry red before quench").
- Steganographic or Base64/Hex encoding in authorized hashes or IDs without schema-level length bounds.
- Human review remains required for full semantic conformance.
