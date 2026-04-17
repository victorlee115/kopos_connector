doc_events = {
    "FB Order": {
        "validate": "kopos_connector.kopos.api.fb_orders.validate_fb_order",
        "before_submit": "kopos_connector.kopos.api.fb_orders.before_submit_fb_order",
        "on_submit": "kopos_connector.kopos.api.fb_orders.on_submit_fb_order",
    },
    "FB Return Event": {
        "validate": "kopos_connector.kopos.api.fb_orders.validate_fb_return_event",
        "on_submit": "kopos_connector.kopos.api.fb_orders.on_submit_fb_return_event",
    },
    "FB Remake Event": {
        "validate": "kopos_connector.kopos.api.fb_orders.validate_fb_remake_event",
        "on_submit": "kopos_connector.kopos.api.fb_orders.on_submit_fb_remake_event",
    },
}
