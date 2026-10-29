/** Great Scale Living Intermediate — shared residual coordinate
 *  Single source of truth for the logarithmic golden cascade.
 *  Used by pure geometry, agent lattice, and attestation layers.
 */
'use strict';

const PHI = (1 + Math.sqrt(5)) / 2;
const PHI_INV = PHI - 1;
const CONE = PHI_INV * PHI_INV;          // φ^{-2}
const FIXED = 4.5;
const RESIDUAL_PARTITION = Object.freeze([4, 3, 1]);
const DUAL_ATLAS = Object.freeze({ 4: 'Watch', 3: 'Observer', 1: 'Forward' });

function heat(h) {
  const x = Math.max(0, Math.min(1, h));
  return x * (1 - x);
}

/** Continuous residual count from heat */
function residualCount(S) {
  return 8 - 4 * S;
}

/** Golden cascade eigenvalue */
function cascadeLambda(n) {
  return Math.pow(PHI, -n);
}

/** Logarithmic coordinate — the single generator */
function logCoordinate(S) {
  return 4 * S;
}

/** Cone-compliant Psi */
function psi(S) {
  return CONE + S;
}

/** Cone check — operational dual of the geometric floor */
function coneCompliant(S) {
  return psi(S) >= CONE - 1e-12;
}

/** Dual-atlas role from residual partition block */
function atlasRole(block) {
  return DUAL_ATLAS[block] || null;
}

/** Map topological saturation / polarization into cascade coordinate */
function fromTopologicalSaturation(tau) {
  // tau ∈ [0,1]; S_T = (1 - tau²)/4
  const S = (1 - tau * tau) / 4;
  return {
    S,
    n: residualCount(S),
    lambda: cascadeLambda(residualCount(S)),
    logCoord: logCoordinate(S),
    psi: psi(S),
    coneOk: coneCompliant(S),
    tau,
  };
}

/** Soft-contraction step on the ninefold circle (GS-LVII) */
function softContract(x, k = 0.5) {
  const m = ((x % 9) + 9) % 9;
  return FIXED + k * (m - FIXED);
}

module.exports = {
  PHI,
  PHI_INV,
  CONE,
  FIXED,
  RESIDUAL_PARTITION,
  DUAL_ATLAS,
  heat,
  residualCount,
  cascadeLambda,
  logCoordinate,
  psi,
  coneCompliant,
  atlasRole,
  fromTopologicalSaturation,
  softContract,
};
