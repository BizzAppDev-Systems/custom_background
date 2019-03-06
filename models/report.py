from odoo import models, fields, api
from odoo.tools.misc import find_in_path
from odoo.tools.translate import _
from odoo.exceptions import UserError

import base64
import logging
import tempfile
import os
import subprocess
from contextlib import closing
from PyPDF2 import PdfFileWriter, PdfFileReader
from reportlab.graphics.barcode import createBarcodeDrawing

try:
    createBarcodeDrawing('Code128', value='foo', format='png', width=100,
                         height=100, humanReadable=1).asString('png')
except Exception:
    pass


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
_logger = logging.getLogger(__name__)


def _get_wkhtmltopdf_bin():
    return find_in_path('wkhtmltopdf')


class IrActionsReportXml(models.Model):
    _inherit = 'ir.actions.report'

    custom_report_background = fields.Boolean(
        string='Custom Report Background')

    @api.model
    def _run_wkhtmltopdf(self, bodies, header=None, footer=None,
                         landscape=False, specific_paperformat_args=None,
                         set_viewport_size=False):
        '''Execute wkhtmltopdf as a subprocess in order to convert html \
        given in input into a pdf
        document.

        :param bodies: The html bodies of the report, one per page.
        :param header: The html header of the report containing all headers.
        :param footer: The html footer of the report containing all footers.
        :param landscape: Force the pdf to be rendered \
        under a landscape format.
        :param specific_paperformat_args: dict of prioritized paperformat\
        arguments.
        :param set_viewport_size: Enable a viewport sized '1024x1280' or\
        '1280x1024' depending of landscape arg.
        :return: Content of the pdf as a string
        '''
        paperformat_id = self.paperformat_id or \
            self.env.user.company_id.paperformat_id

        # Build the base command args for wkhtmltopdf bin
        command_args = self._build_wkhtmltopdf_args(
            paperformat_id,
            landscape,
            specific_paperformat_args=specific_paperformat_args,
            set_viewport_size=set_viewport_size)

        files_command_args = []
        temporary_files = []
        if header:
            head_file_fd, head_file_path = tempfile.mkstemp(
                suffix='.html', prefix='report.header.tmp.')
            with closing(os.fdopen(head_file_fd, 'wb')) as head_file:
                head_file.write(header)
            temporary_files.append(head_file_path)
            files_command_args.extend(['--header-html', head_file_path])
        if footer:
            foot_file_fd, foot_file_path = tempfile.mkstemp(
                suffix='.html', prefix='report.footer.tmp.')
            with closing(os.fdopen(foot_file_fd, 'wb')) as foot_file:
                foot_file.write(footer)
            temporary_files.append(foot_file_path)
            files_command_args.extend(['--footer-html', foot_file_path])

        paths = []
        for i, body in enumerate(bodies):
            prefix = '%s%d.' % ('report.body.tmp.', i)
            body_file_fd, body_file_path = tempfile.mkstemp(suffix='.html',
                                                            prefix=prefix)
            with closing(os.fdopen(body_file_fd, 'wb')) as body_file:
                body_file.write(body)
            paths.append(body_file_path)
            temporary_files.append(body_file_path)

        pdf_report_fd, pdf_report_path = tempfile.mkstemp(suffix='.pdf',
                                                          prefix='report.tmp.')
        os.close(pdf_report_fd)
        temporary_files.append(pdf_report_path)

        try:
            wkhtmltopdf = (
                [_get_wkhtmltopdf_bin()] + command_args + files_command_args +
                paths + [pdf_report_path])
            process = subprocess.Popen(wkhtmltopdf, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
            out, err = process.communicate()

            if process.returncode not in [0, 1]:
                raise UserError(_('Wkhtmltopdf failed (error code: %s). \
                                  Message: %s') % (
                                          str(process.returncode), err))

            if self.custom_report_background:
                temp_back_id, temp_back_path = tempfile.mkstemp(
                    suffix='.pdf', prefix='back_report.tmp.'
                )
                user = self.env['res.users'].browse(self.env.uid)
                if user and user.company_id.custom_report_background_image:
                    back_data = base64.decodestring(
                        user.company_id.custom_report_background_image
                    )
                    with closing(os.fdopen(temp_back_id, 'wb')) as back_file:
                        back_file.write(back_data)
                    temp_report_id, temp_report_path = tempfile.mkstemp(
                        suffix='.pdf',
                        prefix='with_back_report.tmp.'
                    )
                    output = PdfFileWriter()
                    pdf_reader_content = PdfFileReader(pdf_report_path, 'rb')
                    pdf_reader_watermark = PdfFileReader(temp_back_path, 'rb')
                    for i in range(pdf_reader_content.getNumPages()):
                        page = pdf_reader_content.getPage(i)
                        watermark = pdf_reader_watermark.getPage(0)
                        page.mergePage(watermark)
                        output.addPage(page)
                    output.write(open(temp_report_path, 'wb'))
                    pdf_report_path = temp_report_path
                    os.close(temp_report_id)
        except Exception:
            raise

        with open(pdf_report_path, 'rb') as pdf_document:
            pdf_content = pdf_document.read()

        # Manual cleanup of the temporary files
        for temporary_file in temporary_files:
            try:
                os.unlink(temporary_file)
            except (OSError, IOError):
                _logger.error(
                    'Error when trying to remove file %s' % temporary_file)

        return pdf_content

