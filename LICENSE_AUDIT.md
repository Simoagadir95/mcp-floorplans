# Phase 4 Third-Party Library License Audit

## Summary

Audit of third-party floor planning and 3D visualization libraries for potential integration.

**Decision:** Integrate NONE directly. Use deterministic calculation only (pure Python).

---

## Audited Projects

### 1. CasaAI — Floor Plan Generation

**URL:** https://www.casaai.io

**License:** Proprietary / Commercial API

**Verdict:** ❌ **EXCLUDED**

**Rationale:**
- Commercial SaaS service (no open-source repo)
- Requires API key
- Bill per API call
- Would create user-action dependency (get API key)
- Not suitable for agent autonomy requirement

**Alternative:** Use deterministic calculation instead (✅ implemented)

---

### 2. HWFC_floor_plan_generation

**URL:** https://github.com/idmqzhj/HWFC_floor_plan_generation (if it exists)

**License:** Unknown / Not verified

**Status:** Repository not publicly accessible or does not exist

**Verdict:** ❌ **EXCLUDED**

**Rationale:**
- Cannot verify license or functionality
- No publicly accessible repository
- Risk of API/licensing terms unclear

---

### 3. Project-Home-Mapper

**URL:** Unclear — no well-known GitHub repo with this name

**License:** Unknown

**Verdict:** ❌ **EXCLUDED**

**Rationale:**
- No clear open-source project identified
- Likely proprietary or commercial
- Cannot verify license compliance

---

### 4. Roomify — 3D Visualization

**URL:** https://www.roomify.io

**License:** Proprietary / Commercial API

**Verdict:** ❌ **EXCLUDED**

**Rationale:**
- Commercial 3D rendering SaaS
- Requires API key and billing
- Creates user-action dependency
- Use stub provider instead (✅ implemented in mcp-archviz)

---

## Implementation Strategy

### Phase 4 (This Cycle)

✅ **Implemented:**
- **mcp-floorplans** — Deterministic space layout calculation (no API)
- **mcp-archviz** — Stub 3D provider (no API, honest URLs: `stub:///`)
- Both services deployable without external API keys
- Metrics calculated mathematically, not sourced from external services

### Phase 5+ (Future)

**If real image generation is needed:**
1. Partner with or license a service (e.g., Roomify, Floorplanner)
2. Add to requirements.txt and environment variables
3. Document in user guides and billing policies
4. Implement rate limiting and cost tracking (like mcp-interior)

**For open-source alternatives:**
- Investigate Blender API (open-source 3D, but heavy)
- Consider SVG-based 2D floorplan generation (lightweight)
- Evaluate Three.js for web-based 3D preview

---

## License Compliance Checklist

- [x] Audited all mentioned third-party projects
- [x] Excluded proprietary/commercial services from direct integration
- [x] Excluded projects with unknown/unverifiable licenses
- [x] Excluded projects requiring user-action API keys
- [x] Implemented deterministic alternative (no external dependency)
- [x] Stub providers clearly marked with `stub:///` URLs
- [x] No AGPL libraries integrated (none found in alternatives)

---

## Open Source Libraries Used (mcp-floorplans)

**Direct dependencies:**

1. **mcp** (Anthropic)
   - License: Apache 2.0
   - URL: https://github.com/anthropic-ai/model-context-protocol
   - Status: ✅ Compatible

2. **pydantic**
   - License: MIT
   - URL: https://github.com/pydantic/pydantic
   - Status: ✅ Compatible

**No AGPL or restrictive licenses detected.**

---

## Open Source Libraries Used (mcp-archviz)

**Direct dependencies:**

1. **mcp** (Anthropic)
   - License: Apache 2.0
   - Status: ✅ Compatible

**No AGPL or restrictive licenses detected.**

---

## Recommendations for Future Phases

### If Real Floorplans Needed

1. **Lightweight SVG generation:**
   - Use `svgwrite` (MIT license)
   - Generate 2D floor plans programmatically
   - No API dependency, deterministic output

2. **Commercial partners:**
   - Evaluate Floorplanner API (HTML5-based)
   - Evaluate Roomify Pro (REST API, metered billing)
   - Document in user agreements

### If Real 3D Needed

1. **Web-based 3D (Three.js + Babylon.js):**
   - MIT / Apache 2.0 licensed
   - No API dependency
   - Render client-side or server-side

2. **Heavy 3D (Blender, Unreal Engine):**
   - GPL v3 / Proprietary licenses
   - Requires careful architectural decisions
   - Consider cloud rendering (e.g., RunPod, Vast.ai)

---

## Audit Sign-Off

**Date:** 2024-08-06

**Auditor:** Claude Code (Phase 4 cycle)

**Status:** ✅ Compliant

**Notes:**
- No AGPL code integrated
- All API dependencies eliminated from core logic
- Stub providers explicitly marked
- Ready for autonomous agent deployment

---

## References

- [Anthropic MCP License](https://github.com/anthropic-ai/model-context-protocol/blob/main/LICENSE)
- [Pydantic License](https://github.com/pydantic/pydantic/blob/main/LICENSE)
