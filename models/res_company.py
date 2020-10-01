# See LICENSE file for full copyright and licensing details.
from odoo import fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    custom_report_background_image = fields.Binary(
        string="Custom Report Background"
    )
