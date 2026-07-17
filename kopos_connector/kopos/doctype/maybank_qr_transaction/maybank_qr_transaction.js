const MAYBANK_TEST_SIMULATION_CONFIRMATION = "SIMULATE MAYBANK PAYMENT";

function getMaybankSimulationCapability(frm) {
	return (frm.doc.__onload || {}).maybank_qr_simulation || {};
}

frappe.ui.form.on("Maybank QR Transaction", {
	refresh(frm) {
		if (
			frm.is_new()
			|| !(frappe.user_roles || []).includes("System Manager")
		) {
			return;
		}

		const capability = getMaybankSimulationCapability(frm);
		if (capability.already_simulated) {
			frm.set_intro(
				__(
					"TEST PAYMENT: This transaction was paid by the ERP mock simulator. It is not bank settlement evidence."
				),
				"orange"
			);
			return;
		}
		if (!capability.enabled) {
			return;
		}

		frm.set_intro(
			__(
				"TEST MODE: This isolated mock transaction will stay pending until a System Manager explicitly simulates payment. Never enable this mode on production."
			),
			"orange"
		);
		const button = frm.add_custom_button(
			__("Simulate Successful Payment (Test Only)"),
			() => {
				frappe.prompt(
					[
						{
							fieldname: "confirmation",
							fieldtype: "Data",
							label: __("Type SIMULATE MAYBANK PAYMENT to confirm"),
							reqd: 1,
						},
					],
					async (values) => {
						if (values.confirmation !== MAYBANK_TEST_SIMULATION_CONFIRMATION) {
							frappe.msgprint({
								title: __("Confirmation did not match"),
								message: __("No test payment was applied."),
								indicator: "orange",
							});
							return;
						}

						let result;
						try {
							const response = await frappe.call({
								method: "kopos_connector.api.simulate_maybank_qr_payment",
								type: "POST",
								args: {
									transaction_name: frm.doc.name,
									confirmation: values.confirmation,
								},
								btn: button,
								freeze: true,
								freeze_message: __("Applying test Maybank payment..."),
							});
							result = response.message || response;
							if (
								result.status !== "paid"
								|| !["simulated", "already_simulated"].includes(result.result)
								|| result.test_only !== true
							) {
								throw new Error(__("ERP returned an invalid simulation result."));
							}
						} catch (error) {
							frappe.msgprint({
								title: __("Verify the test payment status"),
								message: __(
									"ERP did not return a validated result. Reload this record; if it remains pending, retry safely. The action is idempotent."
								),
								indicator: "orange",
							});
							return;
						}

						try {
							await frm.reload_doc();
						} catch (error) {
							frappe.msgprint({
								title: __("Test payment recorded"),
								message: __(
									"The payment was recorded, but this form could not refresh. Reload the record to see its durable test evidence."
								),
								indicator: "orange",
							});
							return;
						}
						frappe.show_alert(
							{
								message: __("Test Maybank payment recorded; sale finalization is queued."),
								indicator: "green",
							},
							7
						);
					},
					__("Simulate successful payment"),
					__("Apply Test Payment")
				);
			},
			__("Testing")
		);
		button.addClass("btn-danger");
	},
});
