# See LICENSE file for full copyright and licensing details.
import base64
import logging
import os
import subprocess
import tempfile
from contextlib import closing

from PyPDF2 import PdfFileReader, PdfFileWriter
from reportlab.graphics.barcode import createBarcodeDrawing

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools.misc import find_in_path
from odoo.tools.translate import _
from odoo.tools.safe_eval import safe_eval

try:
    createBarcodeDrawing(
        "Code128",
        value="foo",
        format="png",
        width=100,
        height=100,
        humanReadable=1,
    ).asString("png")
except Exception:
    pass


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
_logger = logging.getLogger(__name__)


def _get_wkhtmltopdf_bin():
    return find_in_path("wkhtmltopdf")


class ReportBackgroundLine(models.Model):
    _name = "report.background.line"
    _description = "Report Background Line"

    page_number = fields.Integer()
    type = fields.Selection(
        [
            ("fixed", "Fixed Page"),
            ("expression", "Expression"),
            ("first_page", "First Page"),
            ("last_page", "Last Page"),
            ("remaining", "Remaining pages"),
        ]
    )
    background_pdf = fields.Binary(string="Background PDF")
    report_id = fields.Many2one("ir.actions.report", string="Report")
    page_expression = fields.Char()
    fall_back_to_company = fields.Boolean()


class IrActionsReport(models.Model):
    _inherit = "ir.actions.report"

    custom_report_background = fields.Boolean(string="Custom Report Background")
    custom_report_background_image = fields.Binary(string="Background Image")
    custom_report_type = fields.Selection(
        [
            ("company", "From Company"),
            ("report", "From Report Fixed"),
            ("dynamic", "From Report Dynamic"),
        ]
    )

    background_ids = fields.One2many(
        "report.background.line", "report_id", "Background Configuration"
    )

    def _render_qweb_pdf(self, res_ids=None, data=None):
        Model = self.env[self.model]
        record_ids = Model.browse(res_ids)
        company_id = False
        if record_ids[:1]._name == "res.company":
            company_id = record_ids[:1]
        elif hasattr(record_ids, 'company_id'):
            company_id = record_ids[:1].company_id
        else:
            company_id = self.env.company
        return super(
            IrActionsReport, self.with_context(background_company=company_id)
        )._render_qweb_pdf(res_ids=res_ids, data=data)

    def add_pdf_watermarks(self, custom_background_data, page):
        """create a temp file and set datas and added in report page. #T4209"""
        temp_back_id, temp_back_path = tempfile.mkstemp(
            suffix=".pdf", prefix="back_report.tmp."
        )
        back_data = base64.b64decode(custom_background_data)
        with closing(os.fdopen(temp_back_id, "wb")) as back_file:
            back_file.write(back_data)
        pdf_reader_watermark = PdfFileReader(temp_back_path, "rb")
        watermark_page = pdf_reader_watermark.getPage(0)
        watermark_page.mergePage(page)
        return watermark_page

    @api.model
    def _run_wkhtmltopdf(
        self,
        bodies,
        header=None,
        footer=None,
        landscape=False,
        specific_paperformat_args=None,
        set_viewport_size=False,
    ):
        """Execute wkhtmltopdf as a subprocess in order to convert html given
        in input into a pdf document.

        :param bodies: The html bodies of the report, one per page.
        :param header: The html header of the report containing all headers.
        :param footer: The html footer of the report containing all footers.
        :param landscape: Force the pdf to be rendered under a landscape
                        format.
        :param specific_paperformat_args: dict of prioritized paperformat
                                        arguments.
        :param set_viewport_size: Enable a viewport sized '1024x1280' or
                                '1280x1024' depending of landscape arg.
        :return: Content of the pdf as a string
        """

        # call default odoo standard function of paperformat #19896
        # https://github.com/odoo/odoo/blob/13.0/odoo/addons/base/models/ir_actions_report.py#L243
        paperformat_id = self.get_paperformat()

        # Build the base command args for wkhtmltopdf bin
        command_args = self._build_wkhtmltopdf_args(
            paperformat_id,
            landscape,
            specific_paperformat_args=specific_paperformat_args,
            set_viewport_size=set_viewport_size,
        )

        files_command_args = []
        temporary_files = []
        if header:
            head_file_fd, head_file_path = tempfile.mkstemp(
                suffix=".html", prefix="report.header.tmp."
            )
            with closing(os.fdopen(head_file_fd, "wb")) as head_file:
                head_file.write(header)
            temporary_files.append(head_file_path)
            files_command_args.extend(["--header-html", head_file_path])
        if footer:
            foot_file_fd, foot_file_path = tempfile.mkstemp(
                suffix=".html", prefix="report.footer.tmp."
            )
            with closing(os.fdopen(foot_file_fd, "wb")) as foot_file:
                foot_file.write(footer)
            temporary_files.append(foot_file_path)
            files_command_args.extend(["--footer-html", foot_file_path])

        paths = []
        for i, body in enumerate(bodies):
            prefix = "%s%d." % ("report.body.tmp.", i)
            body_file_fd, body_file_path = tempfile.mkstemp(
                suffix=".html", prefix=prefix
            )
            with closing(os.fdopen(body_file_fd, "wb")) as body_file:
                body_file.write(body)
            paths.append(body_file_path)
            temporary_files.append(body_file_path)

        pdf_report_fd, pdf_report_path = tempfile.mkstemp(
            suffix=".pdf", prefix="report.tmp."
        )
        os.close(pdf_report_fd)
        temporary_files.append(pdf_report_path)
        try:
            wkhtmltopdf = (
                [_get_wkhtmltopdf_bin()]
                + command_args
                + files_command_args
                + paths
                + [pdf_report_path]
            )
            process = subprocess.Popen(
                wkhtmltopdf, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            out, err = process.communicate()

            if process.returncode not in [0, 1]:
                if process.returncode == -11:
                    message = _(
                        "Wkhtmltopdf failed (error code: %s). Memory limit too low or maximum file number of subprocess reached. Message : %s"
                    )
                else:
                    message = _("Wkhtmltopdf failed (error code: %s). Message: %s")
                _logger.warning(message, process.returncode, err[-1000:])
                raise UserError(message % (str(process.returncode), err[-1000:]))
            else:
                if err:
                    _logger.warning("wkhtmltopdf: %s" % err)
            if (
                self
                and self.custom_report_background
                and self.custom_report_type == "dynamic"
            ):
                temp_report_id, temp_report_path = tempfile.mkstemp(
                    suffix=".pdf", prefix="with_back_report.tmp."
                )
                output = PdfFileWriter()
                pdf_reader_content = PdfFileReader(pdf_report_path, "rb")
                first_page = self.background_ids.search(
                    [("type", "=", "first_page"), ("report_id", "=", self.id)],
                    limit=1,
                )
                last_page = self.background_ids.search(
                    [("type", "=", "last_page"), ("report_id", "=", self.id)],
                    limit=1,
                )
                fixed_pages = self.background_ids.search(
                    [("type", "=", "fixed"), ("report_id", "=", self.id)]
                )
                remaining_pages = self.background_ids.search(
                    [("type", "=", "remaining"), ("report_id", "=", self.id)],
                    limit=1,
                )
                expression = self.background_ids.search(
                    [
                        ("type", "=", "expression"),
                        ("report_id", "=", self.id),
                    ],
                    limit=1,
                )
                company_background = self._context.get("background_company")
                company_background_img = (
                    company_background.custom_report_background_image
                )
                for i in range(pdf_reader_content.getNumPages()):
                    watermark = ""
                    if first_page and i == 0:
                        if first_page.fall_back_to_company and company_background:
                            watermark = company_background_img
                        elif fixed_pages.background_pdf:
                            watermark = first_page.background_pdf
                    elif last_page and i == pdf_reader_content.getNumPages() - 1:
                        if last_page.fall_back_to_company and company_background:
                            watermark = company_background_img
                        elif last_page.background_pdf:
                            watermark = last_page.background_pdf
                    elif i + 1 in fixed_pages.mapped("page_number"):
                        fixed_page = fixed_pages.search(
                            [
                                ("page_number", "=", i + 1),
                                ("report_id", "=", self.id),
                            ],
                            limit=1,
                        )
                        if (
                            fixed_page
                            and fixed_page.fall_back_to_company
                            and company_background
                        ):
                            watermark = company_background_img
                        elif fixed_page and fixed_page.background_pdf:
                            watermark = fixed_page.background_pdf
                    elif expression and expression.page_expression:
                        eval_dict = {"page": i + 1}
                        safe_eval(
                            expression.page_expression,
                            eval_dict,
                            mode="exec",
                            nocopy=True,
                        )
                        if (
                            expression.fall_back_to_company
                            and company_background
                            and eval_dict.get("result", False)
                        ):
                            watermark = company_background_img
                        elif (
                            eval_dict.get("result", False) and expression.background_pdf
                        ):
                            watermark = expression.background_pdf
                        else:
                            if remaining_pages:
                                if (
                                    remaining_pages.fall_back_to_company
                                    and company_background
                                ):
                                    watermark = company_background_img
                                elif remaining_pages.background_pdf:
                                    watermark = remaining_pages.background_pdf
                    else:
                        if remaining_pages:
                            if (
                                remaining_pages.fall_back_to_company
                                and company_background
                            ):
                                watermark = company_background_img
                            elif remaining_pages.background_pdf:
                                watermark = remaining_pages.background_pdf
                    if watermark:
                        page = self.add_pdf_watermarks(
                            watermark,
                            pdf_reader_content.getPage(i),
                        )
                    else:
                        page = pdf_reader_content.getPage(i)
                    output.addPage(page)
                output.write(open(temp_report_path, "wb"))
                pdf_report_path = temp_report_path
                os.close(temp_report_id)
            elif self.custom_report_background:
                temp_back_id, temp_back_path = tempfile.mkstemp(
                    suffix=".pdf", prefix="back_report.tmp."
                )

                custom_background = False
                if (
                    self
                    and self.custom_report_background
                    and self.custom_report_type == "report"
                ):
                    custom_background = self.custom_report_background_image
                if (
                    self.custom_report_background
                    and not custom_background
                    and (
                        self.custom_report_type == "company"
                        or not self.custom_report_type
                    )
                    and self._context.get("background_company")  # #19896
                ):
                    # report background will be displayed based on the current
                    # company #19896
                    custom_background = self._context.get(
                        "background_company"
                    ).custom_report_background_image
                if custom_background:
                    back_data = base64.b64decode(custom_background)
                    with closing(os.fdopen(temp_back_id, "wb")) as back_file:
                        back_file.write(back_data)
                    temp_report_id, temp_report_path = tempfile.mkstemp(
                        suffix=".pdf", prefix="with_back_report.tmp."
                    )
                    output = PdfFileWriter()
                    pdf_reader_content = PdfFileReader(pdf_report_path, "rb")

                    for i in range(pdf_reader_content.getNumPages()):
                        page = pdf_reader_content.getPage(i)
                        pdf_reader_watermark = PdfFileReader(temp_back_path, "rb")
                        watermark = pdf_reader_watermark.getPage(0)
                        watermark.mergePage(page)
                        output.addPage(watermark)
                    output.write(open(temp_report_path, "wb"))
                    pdf_report_path = temp_report_path
                    os.close(temp_report_id)
        except Exception as ex:
            logging.info("Error while PDF Background %s" % ex)
            raise

        with open(pdf_report_path, "rb") as pdf_document:
            pdf_content = pdf_document.read()

        # Manual cleanup of the temporary files
        for temporary_file in temporary_files:
            try:
                os.unlink(temporary_file)
            except (OSError, IOError):
                _logger.error("Error when trying to remove file %s" % temporary_file)

        return pdf_content
