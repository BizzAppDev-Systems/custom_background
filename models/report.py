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

try:
    createBarcodeDrawing(
        "Code128", value="foo", format="png", width=100, height=100, humanReadable=1
    ).asString("png")
except Exception:
    pass


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
_logger = logging.getLogger(__name__)


def _get_wkhtmltopdf_bin():
    return find_in_path("wkhtmltopdf")


class IrActionsReport(models.Model):
    _inherit = "ir.actions.report"

    custom_report_background = fields.Boolean(string="Custom Report Background")
    custom_report_background_image = fields.Binary(string="Background Image")
    custom_report_type = fields.Selection(
        [("company", "From Company"), ("report", "From Report")]
    )

    def _render_qweb_pdf(self, res_ids=None, data=None):
        Model = self.env[self.model]
        record_ids = Model.browse(res_ids)
        company_id = False
        if hasattr(record_ids[:1], "company_id"):
            company_id = record_ids[:1].company_id
        return super(
            IrActionsReport, self.with_context(background_company=company_id)
        )._render_qweb_pdf(res_ids=res_ids, data=data)

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
            if self.custom_report_background:
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
