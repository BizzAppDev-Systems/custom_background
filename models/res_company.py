# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.
from odoo import fields, models


class res_company(models.Model):
    _inherit = 'res.company'

    custom_report_background_image = fields.Binary(
        string="Custom Report Background")

# vim:expandtab:smartindent:tabstop=4:softtabstop=4:shiftwidth=4:
