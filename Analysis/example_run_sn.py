from sn_tool import run_sn

# Point this to your file(s)
INPUTS = [r"C:/Cloud Folders/Cloud - Oilfield Formulation/~Raw Test Data~/Shear Coupons/Lab Testing/R&D New Equipment Verification/New_Code_Test_Cyclic - 3.4 .xlsx"]  # replace with your local paths

out = run_sn(
    inputs=INPUTS,
    time_col="Time (s)",
    signal_col="Force (lbf)",
    to_stress_scale=1.0,     # set to 1/Area if converting lbf -> psi
    stress_unit="lbf",
    hysteresis_threshold=2.0,
    msc_method="Goodman",
    goodman_Su=60000,
    export_cycles=True,
    save_prefix="out_vs"
)

print("Outputs written with prefix:", out["save_prefix"])
print("SN fit (if provided inputs for fitting):", out["sn"])
