// Copyright (c) 2026, KoPOS and contributors
// For license information, please see license.txt

frappe.ui.form.on("POS Invoice", {
    refresh: function(frm) {
        if (frm.doc.docstatus === 1) {
            kopos_modifier_ui.add_summary_button(frm);
        }
        kopos_modifier_ui.render_badges(frm);
    }
});

frappe.ui.form.on("POS Invoice Item", {
    items_remove: function(frm, cdt, cdn) {
        kopos_modifier_ui.cleanup(cdn);
    }
});


var kopos_modifier_ui = {
    _cache: new Map(),
    
    add_summary_button: function(frm) {
        var modifierCount = (frm.doc.items || []).reduce(
            function(sum, item) { return sum + (item.custom_kopos_has_modifiers || 0); }, 0
        );
        
        if (modifierCount > 0) {
            frm.add_custom_button(__("Modifier Summary"), function() {
                kopos_modifier_ui.show_summary(frm);
            }, __("View"));
        }
    },
    
    render_badges: function(frm) {
        if (!frm.fields_dict.items || !frm.fields_dict.items.grid) {
            return;
        }
        
        (frm.doc.items || []).forEach(function(item) {
            if (item.custom_kopos_has_modifiers) {
                kopos_modifier_ui._show_badge(frm, item);
            }
        });
    },
    
    _show_badge: function(frm, item) {
        var count = this._get_modifier_count(item);
        if (count === 0) return;
        
        var badge = $('<span class="modifier-badge label label-info" style="margin-left: 8px; cursor: pointer">' +
            '<i class="fa fa-plus-circle"></i> ' + count + ' ' + __("modifiers") + '</span>');
        
        badge.on('click', function() {
            kopos_modifier_ui.show_item_modifiers(item.name, frm);
        });
        
        var $row = this._get_row_element(frm, item.name);
        if ($row) {
            $row.find(".modifier-badge").remove();
            $row.find("div[data-fieldname='item_code']").append(badge);
        }
    },
    
    _get_row_element: function(frm, rowName) {
        var grid = frm.fields_dict.items && frm.fields_dict.items.grid;
        if (!grid) return null;
        
        var escapedName = this._escape_html(rowName);
        return grid.wrapper && grid.wrapper.find('[data-name="' + escapedName + '"]');
    },
    
    _get_modifier_count: function(item) {
        try {
            var snapshot = JSON.parse(item.custom_kopos_modifiers || "{}");
            return snapshot.count || (snapshot.modifiers && snapshot.modifiers.length) || 0;
        } catch (e) {
            return 0;
        }
    },
    
    show_item_modifiers: function(itemName, frm) {
        var item = frm.doc.items.find(function(i) { return i.name === itemName; });
        if (!item) return;
        
        var snapshot = {};
        try {
            snapshot = JSON.parse(item.custom_kopos_modifiers || "{}");
        } catch (e) {
            console.warn("Invalid modifier JSON for item:", itemName);
            return;
        }
        
        var table = $('<table class="table table-bordered">' +
            '<thead><tr>' +
            '<th>' + __("Item") + '</th>' +
            '<th>' + __("Group") + '</th>' +
            '<th class="text-right">' + __("Price") + '</th>' +
            '</tr></thead><tbody></tbody></table>');
        
        if (snapshot.modifiers && snapshot.modifiers.length > 0) {
            var self = this;
            snapshot.modifiers.forEach(function(mod) {
                table.find("tbody").append(
                    $('<tr>' +
                        '<td>' + self._escape_html(mod.name || "-") + '</td>' +
                        '<td>' + self._escape_html(mod.group_name || "-") + '</td>' +
                        '<td class="text-right">' + self._format_currency(mod.price || 0) + '</td>' +
                        '</tr>')
                );
            });
        }
        
        frappe.msgprint({
            title: __("Modifiers for {0}").format(this._escape_html(item.item_name || item.item_code)),
            message: table,
            wide: true
        });
    },
    
    show_summary: function(frm) {
        var items = frm.doc.items.filter(function(i) { return i.custom_kopos_has_modifiers; });
        
        var table = $('<table class="table table-bordered">' +
            '<thead><tr>' +
            '<th>' + __("Item") + '</th>' +
            '<th>' + __("Modifiers") + '</th>' +
            '<th class="text-right">' + __("Total") + '</th>' +
            '</tr></thead><tbody></tbody></table>');
        
        var self = this;
        items.forEach(function(item) {
            var snapshot = {};
            try {
                snapshot = JSON.parse(item.custom_kopos_modifiers || "{}");
            } catch (e) {
                return;
            }
            
            if (snapshot.modifiers && snapshot.modifiers.length > 0) {
                snapshot.modifiers.forEach(function(mod) {
                    table.find("tbody").append(
                        $('<tr>' +
                            '<td>' + self._escape_html(item.item_name) + '</td>' +
                            '<td>' + self._escape_html(mod.name) + ' (' + self._escape_html(mod.group_name) + ')</td>' +
                            '<td class="text-right">' + self._format_currency(mod.price || 0) + '</td>' +
                            '</tr>')
                    );
                });
            }
        });
        
        frappe.msgprint({
            title: __("Modifier Summary"),
            message: table,
            wide: true
        });
    },
    
    _escape_html: function(text) {
        if (!text) return "";
        var div = document.createElement("div");
        div.textContent = text;
        return div.innerHTML;
    },
    
    _format_currency: function(amount) {
        return frappe.format.currency(amount, frappe.boot.currency || "MYR");
    },
    
    reset: function() {
        this._cache.clear();
    },
    
    cleanup: function(rowName) {
        this._cache.delete(rowName);
    }
};
